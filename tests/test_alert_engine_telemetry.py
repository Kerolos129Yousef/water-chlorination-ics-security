"""AlertEngine telemetry-fault lifecycle (Phase 6B) -- pure, synthetic, fast.

Covers the required behaviours for ``AlertEngine.process_health``: a fault opens
on a newly-stuck channel, repeated stuck observations dedup into one incident, the
incident stays active while stuck and closes on the deterministic recovery rule,
multiple channels are tracked independently, the ML (PROCESS_ANOMALY) track is
untouched, the two coexist, and a malformed verdict is rejected.

The engine duck-types on its input, so these push plain ``SimpleNamespace``
objects shaped like ``TelemetryHealthResult`` / ``ChannelHealth`` -- proving the
engine imports nothing from ``simulator``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alerting import (
    AlertEngine,
    AlertStatus,
    PROCESS_ANOMALY_CATEGORY,
    TELEMETRY_FAULT_CATEGORY,
)


def _health(healthy, channels, *, ts=None):
    """A TelemetryHealthResult-like object. ``channels`` = [(feature, unchanged), ...]."""
    stuck = tuple(
        SimpleNamespace(
            feature=f, unchanged_samples=n, staleness_seconds=(n - 1) * 5.0,
            last_value=0.0, reason="STUCK_CHANNEL",
        )
        for f, n in channels
    )
    return SimpleNamespace(
        healthy=healthy,
        stuck_channels=stuck,
        timestamp=ts,
        fault_type=None if healthy else "TELEMETRY_FAULT",
    )


def _detection(is_anomaly, score, threshold=9.269e-5):
    return SimpleNamespace(
        is_anomaly=is_anomaly, anomaly_score=score, threshold=threshold,
        top_features=lambda n: [("LIT101", 0.5), ("FIT101", 0.3)][:n],
    )


# ------------------------------------------------------------------- 1: open


def test_telemetry_fault_opens():
    e = AlertEngine()
    affected = e.process_health(_health(False, [("FIT101", 5)], ts="t1"))
    assert len(affected) == 1
    a = affected[0]
    assert a.category == TELEMETRY_FAULT_CATEGORY
    assert a.status is AlertStatus.OPEN
    assert a.affected_channels == ["FIT101"]
    assert a.is_anomaly is False           # NOT an ML anomaly
    assert a.window_count == 1
    assert a.staleness_seconds == pytest.approx(20.0)
    assert "STUCK_CHANNEL" in a.reason and "FIT101" in a.reason
    assert e.active_telemetry_faults == (a,)


# --------------------------------------------------------------- 2: dedup


def test_repeated_stuck_samples_deduplicate_into_one_fault():
    e = AlertEngine()
    a1 = e.process_health(_health(False, [("FIT101", 5)], ts="t1"))[0]
    a2 = e.process_health(_health(False, [("FIT101", 6)], ts="t2"))[0]
    a3 = e.process_health(_health(False, [("FIT101", 7)], ts="t3"))[0]
    # Same incident object every time -- no duplicate incidents.
    assert a1.alert_id == a2.alert_id == a3.alert_id
    assert a3.window_count == 3
    assert len(e.active_telemetry_faults) == 1
    assert len([x for x in e.alerts if x.category == TELEMETRY_FAULT_CATEGORY]) == 1
    # Span/staleness track the latest observation.
    assert a3.window_end == "t3"
    assert a3.staleness_seconds == pytest.approx(30.0)


# --------------------------------------------------- 3: active while stuck


def test_fault_remains_active_while_stuck():
    e = AlertEngine()
    for i in range(4):
        e.process_health(_health(False, [("AIT201", 5 + i)], ts=f"t{i}"))
        assert len(e.active_telemetry_faults) == 1
        assert e.active_telemetry_faults[0].status is AlertStatus.OPEN


# ----------------------------------------------------- 4: recovery / close


def test_fault_closes_after_recovery():
    e = AlertEngine()
    opened = e.process_health(_health(False, [("FIT101", 5)], ts="t1"))[0]
    # Healthy verdict -> the deterministic recovery rule closes the incident.
    affected = e.process_health(_health(True, [], ts="t2"))
    assert len(affected) == 1
    closed = affected[0]
    assert closed.alert_id == opened.alert_id
    assert closed.status is AlertStatus.CLOSED
    assert closed.closed_at == "t2"
    assert e.active_telemetry_faults == ()
    # History retains the (now closed) incident.
    assert len([a for a in e.alerts if a.category == TELEMETRY_FAULT_CATEGORY]) == 1


def test_reopen_after_recovery_is_a_new_incident():
    e = AlertEngine()
    first = e.process_health(_health(False, [("FIT101", 5)], ts="t1"))[0]
    e.process_health(_health(True, [], ts="t2"))
    second = e.process_health(_health(False, [("FIT101", 5)], ts="t3"))[0]
    assert first.alert_id != second.alert_id       # distinct incidents
    assert len([a for a in e.alerts if a.category == TELEMETRY_FAULT_CATEGORY]) == 2


# --------------------------------------------- 5: multiple channels


def test_multiple_channels_generate_independent_faults():
    e = AlertEngine()
    e.process_health(_health(False, [("FIT101", 5)], ts="t1"))
    affected = e.process_health(_health(False, [("FIT101", 6), ("AIT201", 5)], ts="t2"))
    # One extend (FIT101) + one open (AIT201).
    assert {a.affected_channels[0] for a in affected} == {"FIT101", "AIT201"}
    assert len(e.active_telemetry_faults) == 2
    # Recover only FIT101; AIT201 stays open.
    affected = e.process_health(_health(False, [("AIT201", 7)], ts="t3"))
    closed = [a for a in affected if a.status is AlertStatus.CLOSED]
    assert [c.affected_channels[0] for c in closed] == ["FIT101"]
    assert [x.affected_channels[0] for x in e.active_telemetry_faults] == ["AIT201"]


def test_channels_recover_independently_and_ids_are_unique():
    e = AlertEngine()
    e.process_health(_health(False, [("FIT101", 5), ("AIT201", 5), ("PIT501", 5)], ts="t1"))
    ids = {a.alert_id for a in e.active_telemetry_faults}
    assert len(ids) == 3
    e.process_health(_health(True, [], ts="t2"))
    assert e.active_telemetry_faults == ()
    assert all(a.status is AlertStatus.CLOSED
               for a in e.alerts if a.category == TELEMETRY_FAULT_CATEGORY)


# ------------------------------------------- 6 & 7: ML independence + coexistence


def test_ml_track_is_independent_of_telemetry():
    e = AlertEngine()
    # A telemetry fault must not open, close, or touch the ML active slot.
    e.process_health(_health(False, [("FIT101", 5)], ts="t1"))
    assert e.active_alert is None
    # A normal ML result must not close a telemetry fault.
    assert e.process(_detection(False, 1e-6), window_end="w1") is None
    assert len(e.active_telemetry_faults) == 1


def test_ml_anomaly_and_telemetry_fault_coexist():
    e = AlertEngine()
    ml = e.process(_detection(True, 1e-3), window_start="w0", window_end="w1")
    tel = e.process_health(_health(False, [("FIT101", 5)], ts="t1"))[0]

    assert ml.category == PROCESS_ANOMALY_CATEGORY and ml.is_anomaly is True
    assert tel.category == TELEMETRY_FAULT_CATEGORY and tel.is_anomaly is False
    assert e.active_alert is ml                     # ML slot holds the anomaly
    assert e.active_telemetry_faults == (tel,)      # telemetry track holds the fault
    assert {a.category for a in e.alerts} == {PROCESS_ANOMALY_CATEGORY, TELEMETRY_FAULT_CATEGORY}


# --------------------------------------- 8: existing PROCESS_ANOMALY lifecycle


def test_process_anomaly_lifecycle_unchanged():
    e = AlertEngine()
    a = e.process(_detection(True, 1e-3), window_end="w1")
    assert a.status is AlertStatus.OPEN and a.category == PROCESS_ANOMALY_CATEGORY
    e.process(_detection(True, 2e-3), window_end="w2")     # dedup/extend
    assert a.window_count == 2
    closed = e.process(_detection(False, 1e-6), window_end="w3")
    assert closed is a and closed.status is AlertStatus.CLOSED
    assert e.active_alert is None


def test_reset_clears_both_tracks():
    e = AlertEngine()
    e.process(_detection(True, 1e-3), window_end="w1")
    e.process_health(_health(False, [("FIT101", 5)], ts="t1"))
    e.reset()
    assert e.alerts == ()
    assert e.active_alert is None
    assert e.active_telemetry_faults == ()


# --------------------------------------------- 12: malformed state rejected


@pytest.mark.parametrize(
    "bad",
    [
        SimpleNamespace(healthy=True, stuck_channels=(  # healthy but stuck present
            SimpleNamespace(feature="FIT101", unchanged_samples=5, staleness_seconds=1.0),), timestamp=None),
        SimpleNamespace(healthy=False, stuck_channels=(), timestamp=None),  # unhealthy, none stuck
    ],
)
def test_inconsistent_health_state_is_rejected(bad):
    e = AlertEngine()
    with pytest.raises(ValueError, match="inconsistent"):
        e.process_health(bad)


def test_channel_without_name_is_rejected():
    e = AlertEngine()
    bad = SimpleNamespace(
        healthy=False,
        stuck_channels=(SimpleNamespace(feature="", unchanged_samples=5, staleness_seconds=1.0),),
        timestamp=None,
    )
    with pytest.raises(ValueError, match="feature"):
        e.process_health(bad)


@pytest.mark.parametrize("count", [0, -1, True, 3.5, "5"])
def test_invalid_unchanged_count_is_rejected(count):
    e = AlertEngine()
    bad = SimpleNamespace(
        healthy=False,
        stuck_channels=(SimpleNamespace(feature="FIT101", unchanged_samples=count, staleness_seconds=1.0),),
        timestamp=None,
    )
    with pytest.raises(ValueError, match="unchanged_samples"):
        e.process_health(bad)


def test_missing_healthy_field_is_rejected():
    e = AlertEngine()
    with pytest.raises(ValueError, match="healthy"):
        e.process_health(SimpleNamespace(stuck_channels=()))


# ------------------------- SWaT validation: normal data raises no telemetry fault


@pytest.mark.dataset
def test_normal_swat_segment_opens_no_telemetry_fault(swat_dataset_dir, feature_names):
    """End-to-end guard on real telemetry: streaming the largest contiguous normal
    SWaT segment through the monitor -> AlertEngine.process_health opens ZERO
    telemetry faults at the default 2880-sample threshold. Confirms legitimate
    constant channels are not misreported (Phase 6A default unchanged in 6B)."""
    from types import SimpleNamespace as NS

    from ml.src.dataset import load_attack_v0
    from simulator.telemetry_health import TelemetryHealthMonitor

    series = load_attack_v0(feature_names, swat_dataset_dir, subsample=5)
    labels = series.labels

    # Largest contiguous normal run.
    best = (0, 0)
    n = len(labels)
    i = 0
    while i < n:
        if labels[i] == 0:
            j = i
            while j < n and labels[j] == 0:
                j += 1
            if j - i > best[1] - best[0]:
                best = (i, j)
            i = j
        else:
            i += 1
    start, stop = best
    assert stop - start > 2880  # segment longer than the threshold, so a real test

    monitor = TelemetryHealthMonitor(feature_names)  # defaults: continuous set, 2880
    engine = AlertEngine()
    for k in range(start, stop):
        rec = NS(timestamp=series.timestamps[k], values=series.values[k])
        engine.process_health(monitor.observe(rec))

    assert engine.active_telemetry_faults == ()
    assert [a for a in engine.alerts if a.category == TELEMETRY_FAULT_CATEGORY] == []
