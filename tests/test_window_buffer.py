"""RollingWindow tests -- entirely synthetic, no dataset and no model, fast.

The buffer is duck-typed on its input, so these tests push plain
``SimpleNamespace`` records (``.timestamp`` / ``.values`` / ``.label``) rather than
the replay's ``TelemetryRecord`` -- proving the buffer is not coupled to the
replay. The core properties under test are the streaming windowing contract:
nothing before 30 samples, first window on the 30th, stride 1 thereafter, and
that a caller's arrays are never aliased or mutated.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from simulator.window_buffer import RollingWindow, Window, WindowBufferError

WINDOW = 30
N_FEATURES = 45
FEATS = tuple(f"f{i}" for i in range(N_FEATURES))
T0 = np.datetime64("2015-12-28T10:00:00", "s")


def _rec(i: int, *, label: int = 0, fill: float | None = None, n: int = N_FEATURES):
    """A telemetry-like record. ``fill`` sets every feature; else row = i + feature idx."""
    values = np.full(n, float(fill)) if fill is not None else np.arange(n, dtype=float) + i
    return SimpleNamespace(timestamp=T0 + i * np.timedelta64(5, "s"), values=values, label=label)


# --------------------------------------------------------------- fill behaviour

def test_no_window_before_thirty_samples():
    buf = RollingWindow(FEATS)
    for i in range(WINDOW - 1):
        assert buf.push(_rec(i)) is None
    assert not buf.is_full
    assert buf.n_seen == WINDOW - 1


def test_first_window_appears_exactly_at_sample_thirty():
    buf = RollingWindow(FEATS)
    outputs = [buf.push(_rec(i)) for i in range(WINDOW)]
    assert all(o is None for o in outputs[:-1])
    w = outputs[-1]
    assert isinstance(w, Window)
    assert w.values.shape == (WINDOW, N_FEATURES)
    assert buf.is_full and buf.n_seen == WINDOW


def test_stride_one_advancement():
    """Consecutive windows overlap by 29 rows shifted by one (stride 1)."""
    buf = RollingWindow(FEATS)
    w_prev = None
    for i in range(WINDOW + 5):
        w = buf.push(_rec(i))
        if w is not None and w_prev is not None:
            # window k's rows [:-1] equal window (k-1)'s rows [1:] -- a one-step shift
            np.testing.assert_array_equal(w.values[:-1], w_prev.values[1:])
        w_prev = w


def test_one_window_per_push_after_full():
    buf = RollingWindow(FEATS)
    windows = [buf.push(_rec(i)) for i in range(WINDOW + 5)]
    produced = [w for w in windows if w is not None]
    assert len(produced) == 6  # samples 30..35 -> 6 windows


def test_stream_yields_n_minus_window_plus_one():
    buf = RollingWindow(FEATS)
    n = 100
    windows = list(buf.stream(_rec(i) for i in range(n)))
    assert len(windows) == n - WINDOW + 1
    assert all(w.values.shape == (WINDOW, N_FEATURES) for w in windows)


# ------------------------------------------------------------ content & ordering

def test_feature_order_and_values_preserved():
    buf = RollingWindow(FEATS)
    w = None
    for i in range(WINDOW):
        w = buf.push(_rec(i))
    assert w.feature_names == FEATS
    # row i of the window is the i-th pushed record's values (arange + i)
    for i in range(WINDOW):
        np.testing.assert_array_equal(w.values[i], np.arange(N_FEATURES) + i)


def test_timestamps_preserved_oldest_to_newest():
    buf = RollingWindow(FEATS)
    w = None
    for i in range(WINDOW + 3):  # last window covers samples 3..32
        w = buf.push(_rec(i))
    expected = np.array([T0 + i * np.timedelta64(5, "s") for i in range(3, WINDOW + 3)],
                        dtype="datetime64[s]")
    np.testing.assert_array_equal(w.timestamps, expected)
    assert w.last_timestamp == expected[-1]


def test_label_is_last_timestep_convention():
    buf = RollingWindow(FEATS)
    w = None
    # attack only on the final sample of the window
    for i in range(WINDOW):
        w = buf.push(_rec(i, label=1 if i == WINDOW - 1 else 0))
    assert w.label == 1              # last-timestep label
    assert w.contains_attack is True


def test_contains_attack_but_last_is_normal():
    buf = RollingWindow(FEATS)
    w = None
    for i in range(WINDOW):
        w = buf.push(_rec(i, label=1 if i == 0 else 0))
    assert w.label == 0              # scored (last) timestep is normal
    assert w.contains_attack is True  # but an attack sample is in the window


# --------------------------------------------------------------------- validation

@pytest.mark.parametrize("n", [44, 46, 1])
def test_wrong_feature_count_rejected(n):
    buf = RollingWindow(FEATS)
    with pytest.raises(WindowBufferError, match="expected 45 features"):
        buf.push(_rec(0, n=n))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_rejected(bad):
    buf = RollingWindow(FEATS)
    rec = _rec(0)
    rec.values[7] = bad
    with pytest.raises(WindowBufferError, match="non-finite"):
        buf.push(rec)


def test_non_finite_error_names_the_feature():
    buf = RollingWindow(FEATS)
    rec = _rec(0)
    rec.values[7] = np.nan
    with pytest.raises(WindowBufferError, match="f7"):
        buf.push(rec)


def test_construction_rejects_bad_params():
    with pytest.raises(WindowBufferError, match="window must be >= 1"):
        RollingWindow(FEATS, window=0)
    with pytest.raises(WindowBufferError, match="non-empty"):
        RollingWindow(())


# ---------------------------------------------------------- no aliasing/mutation

def test_push_does_not_mutate_the_input_record():
    buf = RollingWindow(FEATS)
    rec = _rec(0)
    before = rec.values.copy()
    buf.push(rec)
    np.testing.assert_array_equal(rec.values, before)


def test_buffer_stores_copies_not_references():
    """Mutating an original array after push must not change buffered history."""
    buf = RollingWindow(FEATS)
    arrays = [np.full(N_FEATURES, float(i)) for i in range(WINDOW + 1)]
    w = None
    for i in range(WINDOW):
        w = buf.push(SimpleNamespace(timestamp=T0 + i, values=arrays[i], label=0))
    np.testing.assert_array_equal(w.values[:, 0], np.arange(WINDOW))  # 0..29

    arrays[5][:] = 999.0                       # mutate an already-pushed source array
    assert w.values[5, 0] == 5.0               # emitted window is unaffected (fresh copy)

    # internal history is also unaffected: push sample 30 -> new window covers 1..30,
    # old sample 5 now sits at position 4 and must still read 5, not 999.
    w2 = buf.push(SimpleNamespace(timestamp=T0 + WINDOW, values=arrays[WINDOW], label=0))
    assert w2.values[4, 0] == 5.0


def test_returned_window_is_independent_of_later_windows():
    buf = RollingWindow(FEATS)
    w1 = None
    for i in range(WINDOW):
        w1 = buf.push(_rec(i))
    w1.values[0, 0] = -12345.0                 # mutate the returned window
    w2 = buf.push(_rec(WINDOW))                # next window must be uncorrupted
    assert w2.values[-2, 0] != -12345.0


def test_reset_clears_state():
    buf = RollingWindow(FEATS)
    for i in range(WINDOW):
        buf.push(_rec(i))
    assert buf.is_full
    buf.reset()
    assert not buf.is_full and buf.n_seen == 0
    assert buf.push(_rec(0)) is None           # needs 30 pushes again
