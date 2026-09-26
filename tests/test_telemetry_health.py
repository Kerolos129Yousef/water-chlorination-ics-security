"""TelemetryHealthMonitor unit tests -- synthetic, no dataset and no model, fast.

The monitor is duck-typed on its input (``.values`` / optional ``.timestamp``), so
these push plain ``SimpleNamespace`` records rather than the replay's
``TelemetryRecord`` -- proving the monitor is not coupled to the replay. Coverage
targets the phase's required cases: a normal varying channel, a legitimately
stable (discrete) channel, a finite frozen channel, non-finite input, recovery
after a channel resumes, multiple affected channels, the exact detection
boundary, and configuration validation.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from simulator.telemetry_health import (
    DEFAULT_MAX_UNCHANGED_SAMPLES,
    STUCK_CHANNEL,
    TELEMETRY_FAULT,
    ChannelHealth,
    TelemetryHealthError,
    TelemetryHealthMonitor,
    TelemetryHealthResult,
    classify_feature,
    continuous_features,
)

# A small, realistic mixed feature vector: continuous sensors + discrete actuators.
FEATS = ("FIT101", "LIT101", "AIT201", "PIT501", "MV101", "P101", "UV401")
CONT = ("FIT101", "LIT101", "AIT201", "PIT501")  # the default monitored set
T0 = np.datetime64("2015-12-28T10:00:00", "s")


def _rec(values, i: int = 0):
    """A telemetry-like record with a monotonically advancing timestamp."""
    return SimpleNamespace(
        timestamp=T0 + i * np.timedelta64(5, "s"),
        values=np.asarray(values, dtype=float),
    )


def _feed(monitor, rows):
    """Observe a sequence of value-rows; return the list of results."""
    return [monitor.observe(_rec(row, i)) for i, row in enumerate(rows)]


# ------------------------------------------------------------ feature classification


def test_classify_continuous_vs_discrete():
    for name in ("FIT101", "LIT301", "AIT401", "DPIT301", "PIT502"):
        assert classify_feature(name) == "continuous"
    for name in ("MV101", "P204", "P501", "UV401"):
        assert classify_feature(name) == "discrete"


def test_pump_prefix_does_not_swallow_pressure_transmitter():
    # "P" (pump) must not match "PIT" (pressure) -- prefix is the full letter run.
    assert classify_feature("PIT501") == "continuous"
    assert classify_feature("P501") == "discrete"


def test_continuous_features_excludes_actuators_and_preserves_order():
    assert continuous_features(FEATS) == CONT


def test_default_monitored_set_is_the_continuous_sensors():
    m = TelemetryHealthMonitor(FEATS)
    assert m.monitored_features == CONT
    assert m.max_unchanged_samples == DEFAULT_MAX_UNCHANGED_SAMPLES


# --------------------------------------------------------------- normal behaviour


def test_normal_varying_channel_is_healthy():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=5)
    rows = [[float(i), 2.0 + 0.1 * i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(50)]
    results = _feed(m, rows)
    assert all(r.healthy for r in results)
    assert all(r.fault_type is None for r in results)
    assert results[-1].n_seen == 50


def test_legitimately_stable_discrete_channel_is_not_flagged():
    """A pump/valve/UV held constant forever is normal ICS behaviour, never STUCK."""
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=5)
    # Continuous channels vary; the three discrete actuators are pinned constant.
    rows = [[float(i), 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(200)]
    results = _feed(m, rows)
    assert all(r.healthy for r in results)


# ------------------------------------------------------------ stuck detection


def test_finite_frozen_channel_is_flagged():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=5)
    # FIT101 frozen at 0.0; the others keep moving.
    rows = [[0.0, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(10)]
    results = _feed(m, rows)
    # Healthy until the 5th identical sample, then flagged.
    assert [r.healthy for r in results[:4]] == [True, True, True, True]
    assert not results[4].healthy
    r = results[4]
    assert r.fault_type == TELEMETRY_FAULT
    assert r.affected_features == ("FIT101",)
    (ch,) = r.stuck_channels
    assert isinstance(ch, ChannelHealth)
    assert ch.reason == STUCK_CHANNEL
    assert ch.unchanged_samples == 5
    assert ch.last_value == 0.0
    assert STUCK_CHANNEL in r.reason and "FIT101" in r.reason


def test_detection_boundary_is_at_exactly_the_threshold():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    # AIT201 frozen; count reaches 3 on the 3rd sample.
    rows = [[float(i), 2.0 + i, 168.0, 3.0 + i, 2, 1, 0] for i in range(4)]
    results = _feed(m, rows)
    assert results[0].healthy  # count 1
    assert results[1].healthy  # count 2  (one below threshold)
    assert not results[2].healthy  # count 3  (== threshold -> flagged)
    assert not results[3].healthy  # count 4
    assert results[2].stuck_channels[0].unchanged_samples == 3


def test_staleness_seconds_uses_effective_cadence():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3, sample_interval_seconds=5.0)
    rows = [[0.0, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(3)]
    r = _feed(m, rows)[-1]
    ch = r.stuck_channels[0]
    # (unchanged_samples - 1) * interval = (3 - 1) * 5 = 10 s frozen.
    assert ch.staleness_seconds == pytest.approx(10.0)


def test_staleness_seconds_none_when_interval_omitted():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=2, sample_interval_seconds=None)
    r = _feed(m, [[0.0, 1.0, 1.0, 1.0, 2, 1, 0]] * 2)[-1]
    assert r.stuck_channels[0].staleness_seconds is None


def test_multiple_affected_channels():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=4)
    # FIT101 and PIT501 both frozen; LIT101 and AIT201 vary.
    rows = [[0.0, 2.0 + i, 100.0 + i, 7.0, 2, 1, 0] for i in range(4)]
    r = _feed(m, rows)[-1]
    assert not r.healthy
    assert set(r.affected_features) == {"FIT101", "PIT501"}
    assert len(r.stuck_channels) == 2


def test_recovery_after_channel_resumes():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    frozen = [[0.0, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(5)]
    results = _feed(m, frozen)
    assert not results[-1].healthy  # currently stuck
    # FIT101 starts moving again -> immediately healthy, counter reset.
    after = m.observe(_rec([1.0, 99.0, 200.0, 9.0, 2, 1, 0], 5))
    assert after.healthy
    assert after.fault_type is None
    # And it must take the full threshold again to re-trigger, not just one sample.
    again = _feed(m, [[1.0, 99.0, 200.0, 9.0, 2, 1, 0] for _ in range(2)])
    assert not again[-1].healthy
    assert again[-1].stuck_channels[0].unchanged_samples == 3


# ---------------------------------------------------------------- non-finite input


def test_non_finite_never_raises_a_false_stuck():
    """Repeated NaN / inf must not be reported as a stuck channel."""
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    nan_rows = [[np.nan, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(6)]
    assert all(r.healthy for r in _feed(m, nan_rows))
    m.reset()
    inf_rows = [[np.inf, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(6)]
    assert all(r.healthy for r in _feed(m, inf_rows))


def test_non_finite_breaks_an_existing_stuck_run():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    _feed(m, [[0.0, 2.0 + i, 100.0 + i, 3.0 + i, 2, 1, 0] for i in range(3)])
    # A NaN interrupts the frozen run; the count must restart from that break.
    m.observe(_rec([np.nan, 5.0, 5.0, 5.0, 2, 1, 0], 3))
    r1 = m.observe(_rec([0.0, 6.0, 6.0, 6.0, 2, 1, 0], 4))
    assert r1.healthy  # count back to 1
    r2 = m.observe(_rec([0.0, 7.0, 7.0, 7.0, 2, 1, 0], 5))
    assert r2.healthy  # count 2
    r3 = m.observe(_rec([0.0, 8.0, 8.0, 8.0, 2, 1, 0], 6))
    assert not r3.healthy  # count 3 -> flagged again


# ------------------------------------------------------------------ result shape


def test_result_carries_timestamp_and_context():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=2)
    r = m.observe(_rec([0.0, 1.0, 2.0, 3.0, 2, 1, 0], 7))
    assert isinstance(r, TelemetryHealthResult)
    assert r.timestamp == T0 + 7 * np.timedelta64(5, "s")
    assert r.monitored_features == CONT
    assert r.max_unchanged_samples == 2
    assert r.reason is None  # healthy


def test_reset_clears_counters_and_seen():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    _feed(m, [[0.0, 1.0, 1.0, 1.0, 2, 1, 0] for _ in range(3)])
    assert m.n_seen == 3
    m.reset()
    assert m.n_seen == 0
    # After reset the frozen channel needs the full threshold again.
    results = _feed(m, [[0.0, 1.0, 1.0, 1.0, 2, 1, 0] for _ in range(2)])
    assert all(r.healthy for r in results)


def test_two_monitors_are_isolated():
    a = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    b = TelemetryHealthMonitor(FEATS, max_unchanged_samples=3)
    _feed(a, [[0.0, 1.0, 1.0, 1.0, 2, 1, 0] for _ in range(3)])
    rb = b.observe(_rec([0.0, 1.0, 1.0, 1.0, 2, 1, 0], 0))
    assert rb.healthy and rb.n_seen == 1  # b unaffected by a's history


# ------------------------------------------------------------ configuration validation


def test_custom_monitored_features_accepted():
    m = TelemetryHealthMonitor(FEATS, monitored_features=["FIT101"], max_unchanged_samples=2)
    assert m.monitored_features == ("FIT101",)
    # A discrete channel can be monitored if the operator explicitly opts in.
    m2 = TelemetryHealthMonitor(FEATS, monitored_features=["MV101"], max_unchanged_samples=2)
    r = _feed(m2, [[9.0, 9.0, 9.0, 9.0, 2, 1, 0] for _ in range(2)])[-1]
    assert not r.healthy and r.affected_features == ("MV101",)


@pytest.mark.parametrize("bad", [1, 0, -3])
def test_threshold_below_two_is_rejected(bad):
    with pytest.raises(TelemetryHealthError, match="max_unchanged_samples"):
        TelemetryHealthMonitor(FEATS, max_unchanged_samples=bad)


def test_non_integer_threshold_is_rejected():
    with pytest.raises(TelemetryHealthError, match="integer"):
        TelemetryHealthMonitor(FEATS, max_unchanged_samples=3.5)


def test_unknown_monitored_feature_is_rejected():
    with pytest.raises(TelemetryHealthError, match="not in feature_names"):
        TelemetryHealthMonitor(FEATS, monitored_features=["NOPE999"])


def test_duplicate_monitored_feature_is_rejected():
    with pytest.raises(TelemetryHealthError, match="duplicate"):
        TelemetryHealthMonitor(FEATS, monitored_features=["FIT101", "FIT101"])


def test_empty_monitored_set_is_rejected():
    # All-discrete feature vector -> default continuous set is empty -> error.
    with pytest.raises(TelemetryHealthError, match="no features to monitor"):
        TelemetryHealthMonitor(("MV101", "P101", "UV401"))


def test_empty_feature_names_is_rejected():
    with pytest.raises(TelemetryHealthError, match="non-empty"):
        TelemetryHealthMonitor(())


def test_bad_sample_interval_is_rejected():
    with pytest.raises(TelemetryHealthError, match="sample_interval_seconds"):
        TelemetryHealthMonitor(FEATS, sample_interval_seconds=0)


def test_wrong_width_record_is_rejected():
    m = TelemetryHealthMonitor(FEATS, max_unchanged_samples=2)
    with pytest.raises(TelemetryHealthError, match="expected 7 features"):
        m.observe(_rec([0.0, 1.0, 2.0]))  # only 3 values, not 7
