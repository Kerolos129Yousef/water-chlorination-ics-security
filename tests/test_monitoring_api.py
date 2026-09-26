"""Phase 5A monitoring-API tests.

Cover the new read-only surface (`/status`, `/alerts`, `/alerts/active`), its
integration with the in-memory `AlertEngine` via the `/score` flow, CORS, and
that the existing `/health` and `/score` contracts are unchanged. Each test gets
a fresh app (hence a fresh engine) so alert state never leaks between tests.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import DEV_CORS_ORIGINS, create_app
from ml.src.dataset import SWaTSeries
from simulator import SWaTReplay, run_pipeline

T0 = np.datetime64("2015-12-28T10:00:00", "s")
STEP = np.timedelta64(5, "s")


@pytest.fixture
def client(detector):
    """A fresh app+engine per test (isolated alert state)."""
    return TestClient(create_app(detector=detector))


@pytest.fixture
def normal_body(feature_names, normal_window):
    return {
        "feature_names": list(feature_names),
        "window": normal_window.tolist(),
        "window_start": "2015-12-28T10:00:00",
        "window_end": "2015-12-28T10:02:25",
    }


@pytest.fixture
def attack_body(feature_names, attack_window):
    return {
        "feature_names": list(feature_names),
        "window": attack_window.tolist(),
        "window_start": "2015-12-30T09:51:00",
        "window_end": "2015-12-30T09:51:05",
    }


# ------------------------------------------------------------------ /status


def test_status_initial(client, detector):
    body = client.get("/status").json()
    assert client.get("/status").status_code == 200
    assert body["status"] == "ok"
    assert body["detector_loaded"] is True
    assert body["model_type"] == "TranAD"
    assert body["window"] == 30
    assert body["n_features"] == 45
    assert body["threshold"] == pytest.approx(detector.threshold)
    assert body["active_alert_count"] == 0
    assert body["total_alert_count"] == 0
    assert body["last_anomaly_score"] is None
    assert body["last_is_anomaly"] is None
    assert body["alert_state_in_memory"] is True


def test_status_reflects_last_detection(client, normal_body):
    client.post("/score", json=normal_body)
    body = client.get("/status").json()
    assert body["last_is_anomaly"] is False
    assert body["last_anomaly_score"] is not None
    assert body["last_window_end"] == "2015-12-28T10:02:25"


# ----------------------------------------------------------- /alerts (empty)


def test_alerts_empty_initially(client):
    assert client.get("/alerts").json() == []


def test_active_alert_empty_initially(client):
    assert client.get("/alerts/active").json() == {"active_alert": None}


# ----------------------------------------- active alert appears / closes


def test_anomaly_creates_active_alert(client, attack_body):
    r = client.post("/score", json=attack_body)
    assert r.status_code == 200 and r.json()["is_anomaly"] is True

    active = client.get("/alerts/active").json()["active_alert"]
    assert active is not None
    assert active["status"] == "OPEN"
    assert active["is_anomaly"] is True
    assert active["anomaly_score"] > active["threshold"]
    assert client.get("/status").json()["active_alert_count"] == 1
    assert len(client.get("/alerts").json()) == 1


def test_normal_after_anomaly_closes_alert(client, attack_body, normal_body):
    client.post("/score", json=attack_body)
    client.post("/score", json=normal_body)          # return to normal

    assert client.get("/alerts/active").json()["active_alert"] is None
    assert client.get("/status").json()["active_alert_count"] == 0

    history = client.get("/alerts").json()
    assert len(history) == 1                          # the closed alert is retained
    assert history[0]["status"] == "CLOSED"
    assert history[0]["closed_at"] == "2015-12-28T10:02:25"


def test_consecutive_anomalies_are_one_alert(client, attack_body):
    for _ in range(3):
        client.post("/score", json=attack_body)
    assert len(client.get("/alerts").json()) == 1     # deduped, not three
    assert client.get("/alerts/active").json()["active_alert"]["window_count"] == 3


def test_alerts_status_filter(client, attack_body, normal_body):
    client.post("/score", json=attack_body)
    client.post("/score", json=normal_body)          # close #1
    client.post("/score", json=attack_body)          # open #2

    assert len(client.get("/alerts").json()) == 2
    open_only = client.get("/alerts", params={"status": "open"}).json()
    closed_only = client.get("/alerts", params={"status": "closed"}).json()
    assert [a["status"] for a in open_only] == ["OPEN"]
    assert [a["status"] for a in closed_only] == ["CLOSED"]


# ---------------------------------------------------- alert serialization stable


def test_alert_serialization_is_stable(client, attack_body):
    client.post("/score", json=attack_body)
    alert = client.get("/alerts/active").json()["active_alert"]
    expected_keys = {
        "alert_id", "category", "status", "severity", "is_anomaly",
        "anomaly_score", "threshold", "detected_at", "window_start",
        "window_end", "opened_at", "closed_at", "window_count", "top_features",
        # Phase 6B telemetry-fault fields (present on every alert; empty/None here).
        "affected_channels", "reason", "staleness_seconds",
    }
    assert set(alert) == expected_keys
    assert alert["category"] == "PROCESS_ANOMALY"
    # A process anomaly carries no telemetry-fault detail.
    assert alert["affected_channels"] == []
    assert alert["staleness_seconds"] is None
    assert alert["severity"] in {"LOW", "MEDIUM", "HIGH"}     # heuristic band
    assert isinstance(alert["top_features"], list) and len(alert["top_features"]) == 5
    assert set(alert["top_features"][0]) == {"feature", "error"}
    # detector attribution passed through unaltered: canonical top feature first.
    assert isinstance(alert["top_features"][0]["error"], float)


# ------------------------------------------------- existing contracts unchanged


def test_health_still_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["detector_loaded"] is True


def test_score_contract_unchanged(client, normal_body):
    r = client.post("/score", json=normal_body)
    assert r.status_code == 200
    assert set(r.json()) == {
        "anomaly_score", "threshold", "is_anomaly",
        "feature_errors", "feature_names", "window_start", "window_end",
    }


def test_score_still_rejects_bad_window(client, feature_names, normal_window):
    bad = {"feature_names": list(feature_names), "window": normal_window[:29].tolist()}
    assert client.post("/score", json=bad).status_code == 422


# --------------------------------------------------------------------- CORS


def test_cors_allows_dev_origin(client):
    origin = DEV_CORS_ORIGINS[0]
    r = client.get("/status", headers={"origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


def test_cors_not_wildcard(client):
    # An unlisted origin must NOT be echoed as allowed (no silent "*").
    r = client.get("/status", headers={"origin": "http://evil.example.com"})
    assert r.headers.get("access-control-allow-origin") not in ("*", "http://evil.example.com")


# ------------------------------------------------- end-to-end (replay → /alerts/active)


def _series_from_window(values, label, feature_names):
    values = np.asarray(values, dtype=np.float64)
    n = len(values)
    return SWaTSeries(
        values=values,
        labels=np.full(n, label, dtype=np.int8),
        timestamps=(T0 + np.arange(n) * STEP).astype("datetime64[s]"),
        feature_names=tuple(feature_names),
        constant_features=(),
    )


def test_e2e_replay_to_active_alert(client, feature_names, attack_window, normal_window):
    """SWaT replay → 30x45 window → /score → AlertEngine → GET /alerts/active."""

    def scorer(body):
        r = client.post("/score", json=dict(body))
        r.raise_for_status()
        return r.json()

    # Attack replay opens an alert visible on the monitoring endpoint.
    attack_replay = SWaTReplay(_series_from_window(attack_window, 1, feature_names))
    results = list(run_pipeline(attack_replay, scorer))
    assert results and results[0].is_anomaly is True

    active = client.get("/alerts/active").json()["active_alert"]
    assert active is not None and active["status"] == "OPEN"

    # A normal replay window then closes it.
    normal_replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    list(run_pipeline(normal_replay, scorer))
    assert client.get("/alerts/active").json()["active_alert"] is None
    assert client.get("/alerts").json()[0]["status"] == "CLOSED"
