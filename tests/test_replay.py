"""SWaTReplay tests.

Two tiers:

* Synthetic tests build a tiny in-memory :class:`~ml.src.dataset.SWaTSeries` and
  cover ordering, segment selection, the real-time-factor pacing maths,
  determinism and value-copy safety -- no CSVs, fast, run everywhere.
* ``@pytest.mark.dataset`` tests assert the real clean Attack_v0 shape, prove the
  raw CSVs are not modified, and run the full replay → window → detector pipeline
  end to end. Skipped when the licensed CSVs are absent.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ml.src.dataset import SWaTSeries, attack_segments, default_dataset_dir
from simulator import RollingWindow, SWaTReplay, TelemetryRecord
from simulator.swat_replay import ReplayError

N_FEATURES = 45
FEATS = tuple(f"F{i}" for i in range(N_FEATURES))
T0 = np.datetime64("2015-12-28T10:00:00", "s")


def _series(n: int = 100, attack: slice = slice(40, 60)) -> SWaTSeries:
    """Synthetic series: row r has value r in every feature; 5 s cadence."""
    values = np.tile(np.arange(n, dtype=np.float64)[:, None], (1, N_FEATURES))
    labels = np.zeros(n, dtype=np.int8)
    labels[attack] = 1
    timestamps = (T0 + np.arange(n) * np.timedelta64(5, "s")).astype("datetime64[s]")
    return SWaTSeries(
        values=values, labels=labels, timestamps=timestamps,
        feature_names=FEATS, constant_features=(),
    )


# =============================================================== synthetic tests

def test_stream_preserves_order_and_timestamps():
    replay = SWaTReplay(_series(50))
    recs = list(replay.stream(real_time_factor=math.inf))
    assert [r.index for r in recs] == list(range(50))
    ts = [r.timestamp for r in recs]
    assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))
    # row r carried value r
    for r in recs:
        assert r.values[0] == r.index


def test_feature_names_and_count():
    replay = SWaTReplay(_series())
    assert replay.feature_names == FEATS
    assert replay.n_features == N_FEATURES
    assert len(replay.record_at(0).values) == N_FEATURES


def test_record_values_are_copies():
    """Mutating a handed-out record must not reach back into the series."""
    replay = SWaTReplay(_series())
    rec = replay.record_at(0)
    baseline = rec.values.copy()
    rec.values[:] = -999.0
    np.testing.assert_array_equal(replay.record_at(0).values, baseline)


def test_real_time_factor_controls_delay():
    replay = SWaTReplay(_series(5), sample_interval_seconds=5.0)  # 4 gaps
    delays: list[float] = []
    list(replay.stream(real_time_factor=1.0, sleeper=delays.append))
    assert delays == [5.0, 5.0, 5.0, 5.0]                 # real time: full interval
    delays.clear()
    list(replay.stream(real_time_factor=10.0, sleeper=delays.append))
    assert delays == [0.5, 0.5, 0.5, 0.5]                 # 10x faster
    delays.clear()
    list(replay.stream(real_time_factor=100.0, sleeper=delays.append))
    assert delays == pytest.approx([0.05, 0.05, 0.05, 0.05])
    delays.clear()
    list(replay.stream(real_time_factor=math.inf, sleeper=delays.append))
    assert delays == []                                    # no wait at all


def test_first_record_is_immediate():
    replay = SWaTReplay(_series(1), sample_interval_seconds=5.0)
    delays: list[float] = []
    list(replay.stream(real_time_factor=1.0, sleeper=delays.append))
    assert delays == []                                    # single record, no gap


@pytest.mark.parametrize("bad", [0, -1.0, float("nan")])
def test_bad_real_time_factor_rejected(bad):
    replay = SWaTReplay(_series())
    with pytest.raises(ReplayError, match="real_time_factor"):
        list(replay.stream(real_time_factor=bad))


def test_replay_is_deterministic():
    replay = SWaTReplay(_series(80))
    a = list(replay.stream(real_time_factor=math.inf))
    b = list(replay.stream(real_time_factor=math.inf))
    assert [r.index for r in a] == [r.index for r in b]
    for x, y in zip(a, b):
        assert x.timestamp == y.timestamp and x.label == y.label
        np.testing.assert_array_equal(x.values, y.values)


def test_stream_range_bounds_validated():
    replay = SWaTReplay(_series(100))
    with pytest.raises(ReplayError):
        list(replay.stream(start=-1))
    with pytest.raises(ReplayError):
        list(replay.stream(start=50, stop=40))
    with pytest.raises(ReplayError):
        list(replay.stream(stop=1000))


# ------------------------------------------------------------- segment selection

def test_normal_range_selects_only_normal():
    replay = SWaTReplay(_series(100, attack=slice(40, 60)))
    start, stop = replay.normal_range(min_samples=30)
    assert (start, stop) == (0, 40)
    recs = list(replay.stream(start=start, stop=stop, real_time_factor=math.inf))
    assert all(r.label == 0 for r in recs)


def test_attack_range_covers_segment_with_warmup():
    replay = SWaTReplay(_series(100, attack=slice(40, 60)))
    assert replay.attack_segments == [(40, 60)]
    assert replay.longest_attack_index() == 0
    start, stop = replay.attack_range(0, warmup=29)
    assert (start, stop) == (11, 60)
    recs = list(replay.stream(start=start, stop=stop, real_time_factor=math.inf))
    assert recs[0].label == 0                              # warmup is pre-attack
    assert any(r.label == 1 for r in recs)                 # attack is included
    # the 30th sample (first full window's last step) is the attack's first sample
    assert recs[29].index == 40 and recs[29].label == 1


def test_attack_segments_matches_dataset_helper():
    series = _series(100, attack=slice(40, 60))
    replay = SWaTReplay(series)
    assert replay.attack_segments == attack_segments(series.labels)


def test_attack_range_rejects_bad_index():
    replay = SWaTReplay(_series())
    with pytest.raises(ReplayError, match="out of range"):
        replay.attack_range(99)


def test_no_attack_segments_raises():
    replay = SWaTReplay(_series(50, attack=slice(0, 0)))   # all normal
    with pytest.raises(ReplayError, match="no attack segments"):
        replay.attack_range(0)


# ----------------------------------------------- compose replay + rolling window

def test_replay_feeds_rolling_window():
    replay = SWaTReplay(_series(100))
    buf = RollingWindow(replay.feature_names)
    windows = list(buf.stream(replay.stream(real_time_factor=math.inf)))
    assert len(windows) == 100 - 30 + 1
    assert windows[0].values.shape == (30, N_FEATURES)
    # the window's final row is the newest sample it covers
    assert windows[0].values[-1, 0] == 29
    assert windows[-1].values[-1, 0] == 99


def test_telemetry_record_is_frozen():
    rec = SWaTReplay(_series()).record_at(0)
    assert isinstance(rec, TelemetryRecord)
    with pytest.raises(Exception):
        rec.label = 1  # type: ignore[misc]


# ============================================================ real dataset tests

# Snapshot the raw CSVs at import time -- BEFORE any fixture loads them -- so the
# "not modified" test can prove loading/streaming never wrote to disk.
def _csv_snapshot(d):
    out = {}
    for name in ("attack.csv", "normal.csv"):
        p = d / name
        if p.exists():
            st = p.stat()
            out[name] = (st.st_size, st.st_mtime_ns)
    return out


_SNAPSHOT_AT_IMPORT = _csv_snapshot(default_dataset_dir())


@pytest.fixture(scope="session")
def real_replay(swat_dataset_dir, feature_names):
    """Clean Attack_v0 at the calibrated ::5 cadence, loaded once for the session."""
    return SWaTReplay.from_dataset(feature_names, swat_dataset_dir)


@pytest.mark.dataset
class TestRealReplay:
    def test_length_and_attack_ratio(self, real_replay):
        """::5 subsample -> 89,984 samples at the true ~12.15% attack rate."""
        assert len(real_replay) == 89_984
        assert real_replay.attack_ratio == pytest.approx(0.121477, abs=5e-4)

    def test_thirty_five_attack_segments(self, real_replay):
        assert len(real_replay.attack_segments) == 35

    def test_timestamps_non_decreasing(self, real_replay):
        recs = list(real_replay.stream(start=0, stop=3000, real_time_factor=math.inf))
        ts = [r.timestamp for r in recs]
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))

    def test_feature_selection_and_order(self, real_replay, feature_names):
        assert real_replay.feature_names == tuple(feature_names)   # 45, in order
        assert len(real_replay.record_at(0).values) == N_FEATURES

    def test_original_dataset_not_modified(self, swat_dataset_dir, real_replay):
        # real_replay has already loaded (read) the CSVs; streaming reads only RAM.
        list(real_replay.stream(start=0, stop=200, real_time_factor=math.inf))
        assert _SNAPSHOT_AT_IMPORT, "no snapshot captured -- dataset was absent at import"
        assert _csv_snapshot(swat_dataset_dir) == _SNAPSHOT_AT_IMPORT

    def test_end_to_end_with_phase1_detector(self, real_replay, detector):
        """replay -> rolling window -> existing TranAD detector, over an attack segment."""
        assert real_replay.feature_names == detector.feature_names   # alignment
        buf = RollingWindow(detector.feature_names)
        start, stop = real_replay.attack_range(real_replay.longest_attack_index(), warmup=29)

        total = flagged = 0
        for window in buf.stream(
            real_replay.stream(start=start, stop=stop, real_time_factor=math.inf)
        ):
            result = detector.score(window.values)       # window.values is raw (30, 45)
            total += 1
            assert result.feature_errors.shape == (N_FEATURES,)
            assert result.threshold == detector.threshold
            flagged += int(result.is_anomaly)

        assert total == stop - start - 29                # stride-1 window count
        assert flagged > 0                               # the attack is detected at least once
