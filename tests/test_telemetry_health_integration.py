"""Phase 6A integration tests: telemetry-health alongside the real ML pipeline.

These prove the phase's cross-cutting guarantees end-to-end, over the real ASGI
app and the real TranAD detector:

* a telemetry fault is surfaced through ``run_pipeline`` (via an injected monitor);
* attaching the monitor leaves the ML scoring path byte-identical;
* an ML anomaly and a telemetry fault can be present on the SAME window and stay
  distinguishable (separate fields, separate meanings);
* the existing AlertEngine is unaffected -- a telemetry fault never becomes an ML
  alert;
* the backend stays thin and stateless -- telemetry-health is deliberately NOT
  added to ``/status`` (the API sees isolated windows, not the per-sample stream a
  staleness counter needs; per-sample state stays at the ingest layer).

The real golden ``normal`` / ``attack`` windows are reused. They already contain
constant continuous channels (``AIT401`` / ``FIT601`` in the normal window,
``FIT101`` in the attack window -- SWaT stages that park at a baseline), so a
frozen channel is exercised on real data without ever modifying the ML input.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from alerting import AlertEngine
from backend.app import create_app
from ml.src.dataset import SWaTSeries
from simulator import SWaTReplay, TelemetryHealthMonitor, run_pipeline
from simulator.pipeline import EndToEndResult
from simulator.telemetry_health import TELEMETRY_FAULT

T0 = np.datetime64("2015-12-28T10:00:00", "s")
STEP = np.timedelta64(5, "s")


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def app(detector):
    return create_app(detector=detector)


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture
def scorer(client):
    def _score(body):
        r = client.post("/score", json=dict(body))
        r.raise_for_status()
        return r.json()

    return _score


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


# ---------------------------------------------------- 1: telemetry fault surfaced


def test_pipeline_surfaces_a_telemetry_fault(feature_names, normal_window, scorer):
    """A frozen continuous channel (AIT401, constant in the normal window) is
    reported as a TELEMETRY_FAULT on the emitted result."""
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    monitor = TelemetryHealthMonitor(
        feature_names, monitored_features=["AIT401", "FIT601"], max_unchanged_samples=5
    )
    (result,) = list(run_pipeline(replay, scorer, health_monitor=monitor))

    assert isinstance(result, EndToEndResult)
    health = result.telemetry_health
    assert health is not None
    assert health.healthy is False
    assert health.fault_type == TELEMETRY_FAULT
    assert set(health.affected_features) == {"AIT401", "FIT601"}


# --------------------------------------------- 2: ML path unaffected by the monitor


def test_monitor_does_not_alter_ml_scoring(feature_names, normal_window, scorer):
    replay_a = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    replay_b = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    monitor = TelemetryHealthMonitor(feature_names, max_unchanged_samples=5)

    (without,) = list(run_pipeline(replay_a, scorer))
    (withmon,) = list(run_pipeline(replay_b, scorer, health_monitor=monitor))

    # Byte-identical ML decision whether or not the monitor is attached.
    assert without.telemetry_health is None
    assert withmon.anomaly_score == without.anomaly_score
    assert withmon.is_anomaly == without.is_anomaly
    assert withmon.feature_errors == without.feature_errors


# --------------------------------------- 3: ML anomaly + telemetry fault coexist


def test_ml_anomaly_and_telemetry_fault_coexist_and_stay_distinct(
    feature_names, attack_window, scorer
):
    """The attack window scores anomalous AND its FIT101 channel is frozen: both
    signals present on one result, each in its own field."""
    replay = SWaTReplay(_series_from_window(attack_window, 1, feature_names))
    monitor = TelemetryHealthMonitor(
        feature_names, monitored_features=["FIT101"], max_unchanged_samples=5
    )
    (result,) = list(run_pipeline(replay, scorer, health_monitor=monitor))

    # ML side: anomaly. Telemetry side: fault. Independent and both true.
    assert result.is_anomaly is True
    assert result.anomaly_score > result.threshold
    assert result.telemetry_health is not None
    assert result.telemetry_health.healthy is False
    assert result.telemetry_health.affected_features == ("FIT101",)


def test_telemetry_fault_without_ml_anomaly(feature_names, normal_window, scorer):
    """A normal (non-anomalous) window with a frozen channel: telemetry fault, no
    ML anomaly -- the two are genuinely orthogonal."""
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    monitor = TelemetryHealthMonitor(
        feature_names, monitored_features=["AIT401"], max_unchanged_samples=5
    )
    (result,) = list(run_pipeline(replay, scorer, health_monitor=monitor))

    assert result.is_anomaly is False
    assert result.telemetry_health.healthy is False


# ------------------------------------------- 4: AlertEngine behaviour unaffected


def test_telemetry_fault_does_not_create_an_ml_alert(feature_names, normal_window, scorer):
    """Feeding pipeline results (which now carry telemetry_health) into the
    AlertEngine must still open alerts on ML anomalies ONLY."""
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    monitor = TelemetryHealthMonitor(
        feature_names, monitored_features=["AIT401", "FIT601"], max_unchanged_samples=5
    )
    engine = AlertEngine()

    for result in run_pipeline(replay, scorer, health_monitor=monitor):
        assert result.telemetry_health.healthy is False  # telemetry fault present
        alert = engine.process(result)
        assert alert is None  # ...but no ML alert, because is_anomaly is False

    assert engine.alerts == ()
    assert engine.active_alert is None


# --------------------------- 5: backend stays thin -- only a bounded rollup in the API


def test_status_exposes_a_bounded_telemetry_rollup(detector):
    """Phase 6B: /status surfaces a *rollup* of telemetry faults (count + channel
    names) so operators can see them -- but NOT any per-sample streaming state.
    The stuck-channel monitor's counters stay at the ingest layer; the API only
    reports the current fault summary held by the stateful AlertEngine.

    Uses a fresh app/engine so the assertion is about a healthy system (the shared
    module client accumulates faults from the pipeline tests above)."""
    fresh = TestClient(create_app(detector=detector, alert_engine=AlertEngine()))
    body = fresh.get("/status").json()
    # The rollup fields exist and are well-formed on a fresh (healthy) system.
    assert body["active_telemetry_fault_count"] == 0
    assert body["telemetry_fault_channels"] == []
    # No per-sample buffers / raw counters leak into the API surface.
    assert "unchanged_samples" not in body
    assert "stuck_channels" not in body
