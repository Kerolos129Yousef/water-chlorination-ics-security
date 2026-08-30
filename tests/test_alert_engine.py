"""Phase 4 alert-engine tests.

Unit tests drive the engine with tiny synthetic detection results (a fake
exposing exactly the duck-typed surface: ``is_anomaly`` / ``anomaly_score`` /
``threshold`` / ``top_features`` / optional ``window_*``). Two tests use a real
``DetectionResult`` to prove attribution passes through untouched and the input
is never mutated. The integration tests run the real Phase 3 pipeline
(replay → window → HTTP ``/score`` → TranAD) into the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest
from fastapi.testclient import TestClient

from alerting import Alert, AlertEngine, AlertStatus, Severity, severity_for
from alerting.alert import DEFAULT_CATEGORY, HIGH_RATIO, MEDIUM_RATIO
from backend.app import create_app
from ml.src.dataset import SWaTSeries
from simulator import SWaTReplay, run_pipeline

THR = 9.269035945180804e-05
# Ordered (feature, error) attribution, as the detector's top_features would return.
FEATURES = (
    ("AIT201", 0.9), ("P204", 0.5), ("MV304", 0.3),
    ("FIT401", 0.2), ("LIT101", 0.1), ("PIT502", 0.05), ("FIT502", 0.01),
)
T0 = np.datetime64("2015-12-28T10:00:00", "s")
STEP = np.timedelta64(5, "s")


@dataclass
class FakeResult:
    """A minimal stand-in for DetectionResult / EndToEndResult."""

    is_anomaly: bool
    anomaly_score: float
    threshold: float = THR
    window_start: str | None = None
    window_end: str | None = None
    features: tuple = FEATURES
    top_calls: list = field(default_factory=list)

    def top_features(self, n: int) -> list[tuple[str, float]]:
        self.top_calls.append(n)
        return list(self.features[:n])


def _normal(**kw) -> FakeResult:
    return FakeResult(is_anomaly=False, anomaly_score=1.0e-5, **kw)


def _anomaly(score: float = 1.0e-3, **kw) -> FakeResult:
    return FakeResult(is_anomaly=True, anomaly_score=score, **kw)


# ------------------------------------------------------------ 1 & 2: no alert / alert


def test_normal_result_produces_no_alert():
    engine = AlertEngine()
    assert engine.process(_normal()) is None
    assert engine.alerts == ()
    assert engine.active_alert is None


def test_anomaly_result_creates_alert():
    engine = AlertEngine()
    alert = engine.process(_anomaly())
    assert isinstance(alert, Alert)
    assert alert.status is AlertStatus.OPEN
    assert alert.is_anomaly is True
    assert alert.category == DEFAULT_CATEGORY
    assert len(engine.alerts) == 1
    assert engine.active_alert is alert


# --------------------------------------------------- 3, 4, 5: score / threshold / times


def test_alert_contains_anomaly_score():
    alert = AlertEngine().process(_anomaly(score=2.5e-3))
    assert alert.anomaly_score == pytest.approx(2.5e-3)


def test_alert_contains_threshold():
    alert = AlertEngine().process(_anomaly(threshold=THR))
    assert alert.threshold == pytest.approx(THR)


def test_alert_contains_timestamps():
    alert = AlertEngine().process(
        _anomaly(window_start="2015-12-28T10:00:00", window_end="2015-12-28T10:02:25")
    )
    assert alert.window_start == "2015-12-28T10:00:00"
    assert alert.window_end == "2015-12-28T10:02:25"
    assert alert.detected_at == "2015-12-28T10:02:25"   # opening window's end


def test_timestamps_may_be_passed_explicitly():
    """A bare DetectionResult (no window_*) can still carry times via kwargs."""
    alert = AlertEngine().process(
        FakeResult(is_anomaly=True, anomaly_score=1e-3),
        window_start="s", window_end="e",
    )
    assert (alert.window_start, alert.window_end) == ("s", "e")


# ------------------------------------------------------------- 6: top-N attribution


def test_top_n_feature_attribution_default_and_configurable():
    default_alert = AlertEngine().process(_anomaly())
    assert len(default_alert.top_features) == 5                 # default N
    assert default_alert.top_features == list(FEATURES[:5])

    alert3 = AlertEngine(top_n=3).process(_anomaly())
    assert len(alert3.top_features) == 3
    assert alert3.top_features == list(FEATURES[:3])


def test_top_n_must_be_positive():
    with pytest.raises(ValueError):
        AlertEngine(top_n=0)


# ------------------------------------------------------------ 7: deterministic ids


def test_alert_ids_are_deterministic_across_engines():
    seq = [_anomaly(window_end="t1"), _normal(), _anomaly(window_end="t2")]

    def ids_for():
        eng = AlertEngine()
        for r in seq:
            eng.process(r)
        return [a.alert_id for a in eng.alerts]

    first, second = ids_for(), ids_for()
    assert first == second                       # same input → same ids
    assert len(set(first)) == len(first)         # ids are unique within a run


# ------------------------------------------------- 8: consecutive anomalies deduped


def test_consecutive_anomalies_are_deduplicated():
    engine = AlertEngine()
    for _ in range(4):
        engine.process(_anomaly())
    assert len(engine.alerts) == 1               # one logical alert, not four
    assert engine.active_alert.window_count == 4


def test_dedup_tracks_peak_window_for_score_and_attribution():
    engine = AlertEngine()
    engine.process(_anomaly(score=1e-3, features=FEATURES))
    stronger = (("P206", 0.99), ("P204", 0.98), ("MV304", 0.5),
                ("AIT201", 0.4), ("FIT401", 0.2))
    engine.process(_anomaly(score=5e-3, features=stronger))
    engine.process(_anomaly(score=2e-3, features=FEATURES))   # weaker again
    alert = engine.active_alert
    assert alert.window_count == 3
    assert alert.anomaly_score == pytest.approx(5e-3)         # peak, not last
    assert alert.top_features == list(stronger[:5])           # peak window's attribution


# --------------------------------------------------- 9: anomaly → normal closes alert


def test_return_to_normal_closes_active_alert():
    engine = AlertEngine()
    engine.process(_anomaly(window_end="t1"))
    closed = engine.process(_normal(window_end="t2"))
    assert closed is not None
    assert closed.status is AlertStatus.CLOSED
    assert closed.closed_at == "t2"
    assert engine.active_alert is None
    assert len(engine.alerts) == 1


# ------------------------------------------ 10: independent anomalies → independent alerts


def test_independent_anomalies_create_independent_alerts():
    engine = AlertEngine()
    a1 = engine.process(_anomaly(window_end="t1"))
    engine.process(_normal(window_end="t2"))          # closes a1
    a2 = engine.process(_anomaly(window_end="t3"))    # opens a new one
    assert len(engine.alerts) == 2
    assert a1.alert_id != a2.alert_id
    assert a1.status is AlertStatus.CLOSED
    assert a2.status is AlertStatus.OPEN
    assert engine.active_alert is a2


# ---------------------------------------- 11 & 13: attribution & input integrity (real)


def test_attribution_is_not_recomputed_or_altered(detector, attack_window):
    result = detector.score(attack_window)               # real DetectionResult
    expected = result.top_features(5)
    alert = AlertEngine(top_n=5).process(result, window_end="t")
    # Byte-for-byte the detector's own attribution, same order, same values.
    assert alert.top_features == [(n, float(e)) for n, e in expected]


def test_engine_does_not_modify_detection_result(detector, attack_window):
    result = detector.score(attack_window)
    before = (
        float(result.anomaly_score),
        bool(result.is_anomaly),
        float(result.threshold),
        result.feature_errors.copy(),
    )
    AlertEngine().process(result, window_end="t")
    assert float(result.anomaly_score) == before[0]
    assert bool(result.is_anomaly) == before[1]
    assert float(result.threshold) == before[2]
    np.testing.assert_array_equal(result.feature_errors, before[3])


# ------------------------------------------------------------- 12: severity heuristic


def test_severity_is_a_pure_ratio_heuristic():
    assert severity_for(THR * (HIGH_RATIO + 1), THR) is Severity.HIGH
    assert severity_for(THR * (MEDIUM_RATIO + 0.5), THR) is Severity.MEDIUM
    assert severity_for(THR * 1.1, THR) is Severity.LOW
    # Depends ONLY on the score/threshold ratio, never on features (not an ML output).
    a = _anomaly(score=THR * 20, features=FEATURES)
    b = _anomaly(score=THR * 20, features=(("Z", 1.0), ("Y", 0.5)))
    sev_a = AlertEngine().process(a).severity
    sev_b = AlertEngine().process(b).severity
    assert sev_a is sev_b is Severity.HIGH


# ------------------------------------------------------------ 14: state isolation


def test_engine_state_is_isolated_between_instances():
    e1, e2 = AlertEngine(), AlertEngine()
    e1.process(_anomaly())
    assert len(e1.alerts) == 1 and e1.active_alert is not None
    assert e2.alerts == () and e2.active_alert is None    # untouched


def test_reset_clears_all_state():
    engine = AlertEngine()
    engine.process(_anomaly(window_end="t1"))
    engine.reset()
    assert engine.alerts == ()
    assert engine.active_alert is None
    # id counter resets too → ids restart deterministically.
    a = engine.process(_anomaly(window_end="t1"))
    assert a.alert_id == "alert-0001-t1"


# ----------------------------------------------------------- schema serialisation


def test_alert_to_dict_is_json_friendly():
    alert = AlertEngine().process(
        _anomaly(score=THR * 20, window_start="s", window_end="e")
    )
    d = alert.to_dict()
    assert d["status"] == "OPEN"
    assert d["severity"] == "HIGH"
    assert isinstance(d["top_features"], list)
    assert d["top_features"][0] == ["AIT201", pytest.approx(0.9)]
    # every field is a JSON-native type
    import json
    json.loads(json.dumps(d))


# =============================================================== integration (§8)


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


def _scorer(client):
    def score(body):
        r = client.post("/score", json=dict(body))
        r.raise_for_status()
        return r.json()

    return score


def test_integration_pipeline_into_alert_engine(detector, feature_names, normal_window, attack_window):
    """Phase 3 pipeline output → alert engine, on real (golden) SWaT windows."""
    scorer = _scorer(TestClient(create_app(detector=detector)))

    def one(values, label):
        replay = SWaTReplay(_series_from_window(values, label, feature_names))
        return list(run_pipeline(replay, scorer))[0]

    normal_res = one(normal_window, 0)
    attack_res = one(attack_window, 1)
    assert normal_res.is_anomaly is False and attack_res.is_anomaly is True

    engine = AlertEngine()
    sequence = [normal_res, attack_res, attack_res, attack_res, normal_res]
    outs = [engine.process(r) for r in sequence]

    assert outs[0] is None                       # normal window → no alert
    assert len(engine.alerts) == 1               # 3 consecutive attacks → ONE alert
    alert = engine.alerts[0]
    assert alert.window_count == 3               # all three merged
    assert alert.status is AlertStatus.CLOSED    # trailing normal closed it
    assert alert.anomaly_score > alert.threshold
    assert len(alert.top_features) == 5
    assert engine.active_alert is None


@pytest.mark.dataset
def test_integration_real_attack_segment_lifecycle(swat_dataset_dir, detector, feature_names):
    """Full pipeline over a real attack segment then a normal tail: open → dedup → close."""
    replay = SWaTReplay.from_artifacts(dataset_dir=swat_dataset_dir)
    scorer = _scorer(TestClient(create_app(detector=detector)))
    engine = AlertEngine()

    # Attack segment 7 detects 100% (Phase 3 scan); take a few onset windows.
    a_start, a_stop = replay.attack_range(7)
    for r in run_pipeline(replay, scorer, start=a_start, stop=min(a_stop, a_start + 29 + 6)):
        engine.process(r)
    assert engine.active_alert is not None        # attack opened an alert
    assert len(engine.alerts) == 1                # deduped across consecutive windows
    assert engine.active_alert.window_count >= 2

    # A normal tail (fresh buffer) returns to normal and closes it.
    n_start, n_stop = replay.normal_range(min_samples=30)
    for r in run_pipeline(replay, scorer, start=n_start, stop=min(n_stop, n_start + 29 + 3)):
        engine.process(r)
    assert engine.active_alert is None
    assert engine.alerts[0].status is AlertStatus.CLOSED
