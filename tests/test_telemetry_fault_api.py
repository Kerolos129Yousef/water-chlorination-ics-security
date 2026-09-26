"""Phase 6B monitoring-API tests: telemetry faults over the real ASGI app.

Cover the operator-visible surface for TELEMETRY_FAULT: the ``/score`` request
carries an optional ``telemetry_health`` verdict that the backend forwards to the
in-memory AlertEngine; ``/status`` reports a bounded rollup; ``/alerts`` exposes
the new category with affected channels, state, and reason; and the two categories
coexist without interfering. Each test uses a fresh app (isolated engine).
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app

T0 = np.datetime64("2015-12-28T10:00:00", "s")


@pytest.fixture
def client(detector):
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


def _stuck(feature="FIT101", unchanged=2880, ts="2015-12-28T10:02:25"):
    return {
        "healthy": False,
        "fault_type": "TELEMETRY_FAULT",
        "reason": f"STUCK_CHANNEL: {feature}",
        "timestamp": ts,
        "stuck_channels": [
            {
                "feature": feature,
                "unchanged_samples": unchanged,
                "staleness_seconds": (unchanged - 1) * 5.0,
                "last_value": 0.0,
                "reason": "STUCK_CHANNEL",
            }
        ],
    }


def _healthy(ts="2015-12-28T10:02:30"):
    return {"healthy": True, "fault_type": None, "reason": None,
            "timestamp": ts, "stuck_channels": []}


# ------------------------------------------------------ score contract unchanged


def test_score_response_contract_unchanged_with_health(client, normal_body):
    """Attaching telemetry_health must not change the /score response shape."""
    body = {**normal_body, "telemetry_health": _healthy()}
    r = client.post("/score", json=body)
    assert r.status_code == 200
    assert set(r.json()) == {
        "anomaly_score", "threshold", "is_anomaly",
        "feature_errors", "feature_names", "window_start", "window_end",
    }


def test_score_still_works_without_health(client, normal_body):
    assert client.post("/score", json=normal_body).status_code == 200


def test_malformed_health_payload_is_rejected_422(client, normal_body):
    # unchanged_samples < 1 violates the schema (ge=1) -> 422, score not corrupted.
    bad = {**normal_body, "telemetry_health": {"healthy": False, "stuck_channels": [
        {"feature": "FIT101", "unchanged_samples": 0}]}}
    assert client.post("/score", json=bad).status_code == 422


# ----------------------------------------------------------------- /status rollup


def test_status_reports_active_telemetry_fault(client, normal_body):
    client.post("/score", json={**normal_body, "telemetry_health": _stuck("AIT201")})
    body = client.get("/status").json()
    assert body["active_telemetry_fault_count"] == 1
    assert body["telemetry_fault_channels"] == ["AIT201"]
    # ...and the ML rollup is unaffected (no ML anomaly on the normal window).
    assert body["active_alert_count"] == 0


def test_status_rollup_clears_after_recovery(client, normal_body):
    client.post("/score", json={**normal_body, "telemetry_health": _stuck("FIT101")})
    client.post("/score", json={**normal_body, "telemetry_health": _healthy()})
    body = client.get("/status").json()
    assert body["active_telemetry_fault_count"] == 0
    assert body["telemetry_fault_channels"] == []


# ------------------------------------------------ /alerts category + channel info


def test_alerts_expose_category_state_and_channels(client, normal_body):
    client.post("/score", json={**normal_body, "telemetry_health": _stuck("FIT101", 3000)})
    faults = client.get("/alerts?category=TELEMETRY_FAULT").json()
    assert len(faults) == 1
    a = faults[0]
    assert a["category"] == "TELEMETRY_FAULT"
    assert a["status"] == "OPEN"
    assert a["affected_channels"] == ["FIT101"]
    assert a["is_anomaly"] is False
    assert a["staleness_seconds"] == pytest.approx(2999 * 5.0)
    assert "STUCK_CHANNEL" in a["reason"]


def test_alerts_category_filter_separates_the_two_kinds(client, normal_body, attack_body):
    client.post("/score", json={**attack_body, "telemetry_health": _healthy()})   # ML anomaly
    client.post("/score", json={**normal_body, "telemetry_health": _stuck("FIT101")})  # telemetry

    anomalies = client.get("/alerts?category=PROCESS_ANOMALY").json()
    faults = client.get("/alerts?category=TELEMETRY_FAULT").json()
    assert len(anomalies) == 1 and anomalies[0]["category"] == "PROCESS_ANOMALY"
    assert len(faults) == 1 and faults[0]["category"] == "TELEMETRY_FAULT"
    # Unfiltered returns both.
    assert len(client.get("/alerts").json()) == 2


def test_active_alert_endpoint_stays_ml_only(client, normal_body):
    """/alerts/active is the ML active slot; a telemetry fault must not appear there."""
    client.post("/score", json={**normal_body, "telemetry_health": _stuck("FIT101")})
    assert client.get("/alerts/active").json()["active_alert"] is None


# --------------------------------------------------------------- coexistence


def test_ml_anomaly_and_telemetry_fault_coexist_over_api(client, attack_body):
    """One window: ML anomaly AND a stuck channel -> both surfaced, both distinct."""
    client.post("/score", json={**attack_body, "telemetry_health": _stuck("FIT101")})
    body = client.get("/status").json()
    assert body["last_is_anomaly"] is True             # ML anomaly present
    assert body["active_alert_count"] == 1             # ML alert open
    assert body["active_telemetry_fault_count"] == 1   # telemetry fault open too
    cats = {a["category"] for a in client.get("/alerts").json()}
    assert cats == {"PROCESS_ANOMALY", "TELEMETRY_FAULT"}


# --------------------------------------------------- in-memory limitation intact


def test_state_is_in_memory_and_resets_with_a_fresh_app(detector, normal_body):
    """The documented Phase 4/5A limitation still holds for telemetry faults:
    alert state lives in memory and a new app starts clean (no persistence)."""
    app1 = TestClient(create_app(detector=detector))
    app1.post("/score", json={**normal_body, "telemetry_health": _stuck("FIT101")})
    assert app1.get("/status").json()["active_telemetry_fault_count"] == 1
    assert app1.get("/status").json()["alert_state_in_memory"] is True

    # A brand-new app == a process restart: no carried-over telemetry state.
    app2 = TestClient(create_app(detector=detector))
    body2 = app2.get("/status").json()
    assert body2["active_telemetry_fault_count"] == 0
    assert body2["total_alert_count"] == 0
