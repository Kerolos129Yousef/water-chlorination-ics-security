"""FastAPI backend tests (Phase 2B).

Thin-boundary tests only -- they exercise the API contract, not TranAD's numerics
(Phase 1 golden-vector tests own that). The real detector is reused via the
session-scoped ``detector`` fixture from conftest, so no model reload and no
dataset are needed. Two lightweight stand-ins cover the paths a real detector
can't easily produce on demand: a *spy* (to prove the detector is invoked) and a
*failing* detector (to prove a controlled 500).

The valid window is the Phase 1 golden ``normal`` window (raw 30x45), so a "valid
score" here is anchored to the same data the rest of the suite trusts.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest
from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.app import create_app

# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def client(detector):
    """API backed by the shared, already-loaded real detector."""
    return TestClient(create_app(detector=detector))


@pytest.fixture
def valid_body(feature_names, normal_window):
    """A well-formed request: canonical names + the golden normal 30x45 window."""
    return {"feature_names": list(feature_names), "window": normal_window.tolist()}


class _SpyDetector:
    """Delegates to the real detector but counts score() calls."""

    def __init__(self, real):
        self._real = real
        self.score_calls = 0

    @property
    def feature_names(self):
        return self._real.feature_names

    def score(self, window):
        self.score_calls += 1
        return self._real.score(window)


class _FailingDetector:
    """Passes feature validation, then raises during scoring."""

    def __init__(self, feature_names):
        self._fn = tuple(feature_names)

    @property
    def feature_names(self):
        return self._fn

    def score(self, window):
        raise RuntimeError("simulated inference failure")


# ----------------------------------------------------------------------- /health


def test_health(client, detector):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["detector_loaded"] is True
    assert body["model_type"] == "TranAD"
    assert body["window"] == 30
    assert body["n_features"] == 45
    assert body["threshold"] == pytest.approx(detector.threshold)
    assert body["threshold_metadata"]["percentile"] == 99
    assert body["threshold_caveat"]  # non-empty diagnostic string


def test_openapi_docs_available(client):
    """FastAPI's built-in /docs and schema must be reachable."""
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_health_lazy_loads_detector(monkeypatch, detector):
    """Production path (no injected detector): /health loads it via from_artifacts.

    Exercises ``create_app(detector=None)`` explicitly rather than the injected
    fixture, so the lazy-load branch in ``get_detector`` is actually covered. We
    stub ``from_artifacts`` to hand back the shared real detector (avoids a second
    model load) and assert it was invoked exactly once and then cached.
    """
    calls = {"n": 0}

    def fake_from_artifacts(*args, **kwargs):
        calls["n"] += 1
        return detector

    monkeypatch.setattr(backend_app.TranADDetector, "from_artifacts", fake_from_artifacts)
    client = TestClient(create_app(detector=None))

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["detector_loaded"] is True
    assert body["model_type"] == "TranAD"
    assert body["window"] == 30
    assert body["n_features"] == 45
    assert body["threshold"] == pytest.approx(detector.threshold)
    assert body["threshold_metadata"]["percentile"] == 99
    assert body["threshold_caveat"]
    assert calls["n"] == 1  # loaded lazily via the production loader...

    client.get("/health")
    assert calls["n"] == 1  # ...and cached on app.state, not reloaded per request


def test_health_reports_503_when_load_fails(monkeypatch):
    """A detector-load failure is reported honestly as 503, with no leakage.

    ``from_artifacts`` raises with an internal, secret-looking message; the
    endpoint must return 503 / ``unavailable`` / ``detector_loaded=False`` and
    the body must not carry the exception text, path, or a traceback.
    """
    secret = "artifacts missing on disk: /secret/path/model.pt"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(backend_app.TranADDetector, "from_artifacts", boom)
    client = TestClient(create_app(detector=None))

    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["detector_loaded"] is False
    assert body["model_type"] == "TranAD"
    assert body["detail"] == "TranAD detector is not loaded"
    # detector-derived diagnostics are null on the degraded path
    assert body["window"] is None
    assert body["n_features"] is None
    assert body["threshold"] is None
    assert body["threshold_metadata"] is None
    # no internal detail, path, exception type, or traceback leaks to the client
    text = r.text
    assert secret not in text
    assert "/secret/path/model.pt" not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text


# ------------------------------------------------------------------ valid /score


def test_score_valid_window(client, valid_body, feature_names):
    # the request is exactly 30 timesteps x 45 features
    assert len(valid_body["window"]) == 30
    assert all(len(row) == 45 for row in valid_body["window"])

    r = client.post("/score", json=valid_body)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "anomaly_score", "threshold", "is_anomaly",
        "feature_errors", "feature_names", "window_start", "window_end",
    }
    assert isinstance(body["anomaly_score"], float)
    assert isinstance(body["threshold"], float)
    assert isinstance(body["is_anomaly"], bool)
    assert body["feature_names"] == list(feature_names)
    assert len(body["feature_errors"]) == 45


def test_timestamps_pass_through(client, valid_body):
    valid_body["window_start"] = "2015-12-28T10:00:00"
    valid_body["window_end"] = "2015-12-28T10:02:25"
    body = client.post("/score", json=valid_body).json()
    assert body["window_start"] == "2015-12-28T10:00:00"
    assert body["window_end"] == "2015-12-28T10:02:25"


# --------------------------------------------------------------- input rejection


def test_reject_29_timesteps(client, valid_body):
    valid_body["window"] = valid_body["window"][:29]
    assert client.post("/score", json=valid_body).status_code == 422


def test_reject_31_timesteps(client, valid_body):
    valid_body["window"].append(valid_body["window"][-1])
    assert client.post("/score", json=valid_body).status_code == 422


def test_reject_missing_feature(client, feature_names, normal_window):
    body = {
        "feature_names": list(feature_names)[:-1],       # 44 names
        "window": normal_window[:, :-1].tolist(),        # 44 values/row -> shape-consistent
    }
    r = client.post("/score", json=body)
    assert r.status_code == 422
    assert "missing" in r.text.lower()


def test_reject_unknown_feature(client, feature_names, normal_window):
    names = list(feature_names)
    names[-1] = "TOTALLY_NOT_A_FEATURE"
    r = client.post("/score", json={"feature_names": names, "window": normal_window.tolist()})
    assert r.status_code == 422
    assert "unknown" in r.text.lower()


def test_reject_duplicate_feature(client, feature_names, normal_window):
    names = list(feature_names)
    names[1] = names[0]                                  # duplicate; length still 45
    r = client.post("/score", json={"feature_names": names, "window": normal_window.tolist()})
    assert r.status_code == 422
    assert "duplicate" in r.text.lower()


def test_reject_incorrect_feature_count(client, feature_names, normal_window):
    # 45 names declared, but each timestep carries only 44 values
    body = {"feature_names": list(feature_names), "window": normal_window[:, :-1].tolist()}
    assert client.post("/score", json=body).status_code == 422


def test_reject_features_out_of_order(client, feature_names, normal_window):
    """Same 45 names, swapped order -> rejected (no silent reordering)."""
    names = list(feature_names)
    names[0], names[1] = names[1], names[0]
    r = client.post("/score", json={"feature_names": names, "window": normal_window.tolist()})
    assert r.status_code == 422
    assert "order" in r.text.lower()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_non_finite(client, valid_body, bad):
    # httpx refuses to serialize non-finite floats, so build the raw JSON body
    # ourselves (stdlib json emits the NaN/Infinity tokens Starlette will parse).
    valid_body["window"][0][0] = bad
    raw = json.dumps(valid_body)
    r = client.post("/score", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert "non-finite" in r.text.lower()


def test_reject_malformed_json(client):
    r = client.post(
        "/score", content="{ this is not valid json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_reject_unknown_top_level_field(client, valid_body):
    valid_body["surprise"] = 1
    assert client.post("/score", json=valid_body).status_code == 422


# ------------------------------------------------------ semantics of the response


def test_deterministic_response(client, valid_body):
    r1 = client.post("/score", json=valid_body).json()
    r2 = client.post("/score", json=valid_body).json()
    assert r1 == r2


def test_threshold_decision(client, feature_names, normal_window, attack_window):
    names = list(feature_names)
    normal = client.post("/score", json={"feature_names": names, "window": normal_window.tolist()}).json()
    attack = client.post("/score", json={"feature_names": names, "window": attack_window.tolist()}).json()

    assert normal["is_anomaly"] is False                 # golden normal scores below
    assert attack["is_anomaly"] is True                  # golden attack scores above
    # decision is exactly score > threshold, strictly
    assert normal["is_anomaly"] == (normal["anomaly_score"] > normal["threshold"])
    assert attack["is_anomaly"] == (attack["anomaly_score"] > attack["threshold"])


def test_per_feature_attribution(client, valid_body, feature_names):
    fe = client.post("/score", json=valid_body).json()["feature_errors"]
    assert len(fe) == 45
    assert [x["feature"] for x in fe] == list(feature_names)   # canonical order preserved
    assert all(math.isfinite(x["error"]) and x["error"] >= 0.0 for x in fe)


# ---------------------------------------------- integration / failure / integrity


def test_detector_is_invoked(detector, feature_names, normal_window):
    spy = _SpyDetector(detector)
    client = TestClient(create_app(detector=spy))
    r = client.post("/score", json={"feature_names": list(feature_names), "window": normal_window.tolist()})
    assert r.status_code == 200
    assert spy.score_calls == 1
    # and the API reports exactly the detector's own number
    assert r.json()["anomaly_score"] == pytest.approx(float(detector.score(normal_window).anomaly_score))


def test_controlled_inference_failure(feature_names, normal_window):
    client = TestClient(create_app(detector=_FailingDetector(feature_names)))
    r = client.post("/score", json={"feature_names": list(feature_names), "window": normal_window.tolist()})
    assert r.status_code == 500
    assert r.json() == {"detail": "inference failed"}    # controlled, no traceback


def test_api_does_not_mutate_artifacts(client, valid_body, artifacts_dir):
    def digest():
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(artifacts_dir.iterdir()) if p.is_file()
        }

    before = digest()
    client.get("/health")
    client.post("/score", json=valid_body)                       # valid
    bad = {"feature_names": valid_body["feature_names"], "window": valid_body["window"][:5]}
    client.post("/score", json=bad)                              # rejected
    assert digest() == before
