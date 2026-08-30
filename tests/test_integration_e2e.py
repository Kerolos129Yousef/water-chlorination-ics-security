"""Phase 3 end-to-end integration tests.

These exercise the WHOLE path, not any component in isolation::

    SWaTReplay ─▶ RollingWindow ─▶ window_to_request ─▶ POST /score ─▶ TranAD ─▶ EndToEndResult

Transport. The "HTTP boundary" here is FastAPI's :class:`TestClient`, which drives
the real ASGI app -- the same validation, the same request/response
serialisation -- in-process. No socket and, crucially, no waiting: the pipeline
runs with ``real_time_factor=inf`` so a 30-sample window never costs 150 s.

Data. Detection is asserted only on **real** SWaT-derived data: the committed
Phase 1 golden ``normal`` / ``attack`` windows (30x45 raw), streamed one sample
at a time so the rolling buffer reconstructs exactly that window. Structural
properties (warm-up, stride, timestamps, pacing) use tiny synthetic streams --
fast, and no dataset required.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from ml.src.dataset import SWaTSeries
from simulator import RollingWindow, SWaTReplay, run_pipeline, window_to_request
from simulator.pipeline import EndToEndResult

N_FEATURES = 45
T0 = np.datetime64("2015-12-28T10:00:00", "s")
STEP = np.timedelta64(5, "s")  # provenance: SUBSAMPLE=5 over a 1 Hz source


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def app(detector):
    """The real ASGI app over the shared, already-loaded detector."""
    return create_app(detector=detector)


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture
def scorer(client):
    """A Scorer that POSTs over the (in-process) HTTP boundary and returns JSON."""

    def _score(body):
        r = client.post("/score", json=dict(body))
        r.raise_for_status()
        return r.json()

    return _score


class _CountingScorer:
    """Wraps a scorer to prove exactly when (and how often) the API is hit."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def __call__(self, body):
        self.calls += 1
        return self._inner(body)


# --------------------------------------------------------------------- helpers


def _series_from_window(values, label, feature_names, t0=T0):
    """A SWaTSeries whose rows ARE the given 30x45 window, all one label.

    Streamed through a 30-sample RollingWindow it yields exactly one window equal
    to ``values`` -- so a golden window makes a full round trip through the real
    pipeline. ``constant_features`` is metadata only (unused by replay / buffer /
    detector), so ``()`` is correct here.
    """
    values = np.asarray(values, dtype=np.float64)
    n = len(values)
    return SWaTSeries(
        values=values,
        labels=np.full(n, label, dtype=np.int8),
        timestamps=(t0 + np.arange(n) * STEP).astype("datetime64[s]"),
        feature_names=tuple(feature_names),
        constant_features=(),
    )


def _synth_series(n, feature_names, attack=slice(0, 0), t0=T0):
    """A tiny synthetic stream for structural checks (values are not real SWaT)."""
    values = np.tile(np.arange(n, dtype=np.float64)[:, None], (1, len(feature_names)))
    labels = np.zeros(n, dtype=np.int8)
    labels[attack] = 1
    return SWaTSeries(
        values=values,
        labels=labels,
        timestamps=(t0 + np.arange(n) * STEP).astype("datetime64[s]"),
        feature_names=tuple(feature_names),
        constant_features=(),
    )


# ------------------------------------------------ 1 & 2: real normal / attack path


def test_e2e_normal_segment_classified_normal(feature_names, normal_window, scorer):
    """NORMAL: replay → window → API → TranAD → not anomalous."""
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    results = list(run_pipeline(replay, scorer))

    assert len(results) == 1                       # 30 rows -> exactly one window
    result = results[0]
    assert isinstance(result, EndToEndResult)
    assert result.is_anomaly is False              # golden normal scores below threshold
    assert result.anomaly_score <= result.threshold
    assert result.ground_truth_label == 0
    assert len(result.feature_errors) == 45


def test_e2e_attack_segment_flagged_anomaly(feature_names, attack_window, scorer):
    """ATTACK: the SAME pipeline, same rules, flags the anomaly."""
    replay = SWaTReplay(_series_from_window(attack_window, 1, feature_names))
    results = list(run_pipeline(replay, scorer))

    assert len(results) == 1
    result = results[0]
    assert result.is_anomaly is True               # golden attack scores above threshold
    assert result.anomaly_score > result.threshold
    assert result.ground_truth_label == 1
    assert result.contains_attack is True


# ------------------------------------------------------------ 3: 30-sample warm-up


def test_warmup_emits_no_window_before_thirty_samples(feature_names, scorer):
    counting = _CountingScorer(scorer)

    # 29 samples: buffer never fills -> no window, no API call.
    replay29 = SWaTReplay(_synth_series(29, feature_names))
    assert list(run_pipeline(replay29, counting)) == []
    assert counting.calls == 0

    # The 30th sample produces the first (and only) window.
    replay30 = SWaTReplay(_synth_series(30, feature_names))
    results = list(run_pipeline(replay30, counting))
    assert len(results) == 1
    assert counting.calls == 1


# ------------------------------------------------- 4: rolling stride = 1 across calls


def test_rolling_stride_one_across_api_calls(feature_names, scorer):
    n = 35
    counting = _CountingScorer(scorer)
    replay = SWaTReplay(_synth_series(n, feature_names))
    results = list(run_pipeline(replay, counting))

    assert len(results) == n - 30 + 1              # 6 windows
    assert counting.calls == len(results)          # one API call per emitted window

    # Each successive window is the previous one advanced by exactly one sample.
    ends = [np.datetime64(r.window_end) for r in results]
    diffs = np.diff(ends)
    assert all(d == STEP for d in diffs)


# ------------------------------------------------------------ 5: timestamp propagation


def test_timestamp_propagation_end_to_end(feature_names, scorer):
    replay = SWaTReplay(_synth_series(30, feature_names))
    result = list(run_pipeline(replay, scorer))[0]

    # First window spans samples 0..29; its bounds must survive the round trip.
    assert result.window_start == str(T0)
    assert result.window_end == str(T0 + 29 * STEP)


# --------------------------------------------------------- 6: feature ordering intact


def test_feature_ordering_preserved(feature_names, normal_window, scorer):
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    result = list(run_pipeline(replay, scorer))[0]

    canonical = list(feature_names)
    assert list(result.feature_names) == canonical
    assert [name for name, _ in result.feature_errors] == canonical


# ----------------------------------------- 7: API result matches the direct detector


def test_api_result_matches_direct_detector(detector, feature_names, normal_window, scorer):
    """The window that reaches the API, scored directly, gives the same numbers."""
    replay = SWaTReplay(_series_from_window(normal_window, 0, feature_names))
    buf = RollingWindow(feature_names)
    window = None
    for record in replay.stream(real_time_factor=math.inf):  # no-wait: don't really sleep
        window = buf.push(record)
    assert window is not None

    # Same window, two paths: HTTP and in-process detector.
    api = _result_from_body(window_to_request(window), scorer)
    direct = detector.score(window.values)

    assert api["anomaly_score"] == pytest.approx(float(direct.anomaly_score), rel=1e-6)
    assert api["threshold"] == pytest.approx(float(direct.threshold))
    assert api["is_anomaly"] is bool(direct.is_anomaly)
    api_errors = np.array([fe["error"] for fe in api["feature_errors"]])
    np.testing.assert_allclose(api_errors, direct.feature_errors, rtol=1e-6, atol=1e-12)


def _result_from_body(body, scorer):
    return scorer(body)


# ---------------------------------------------------- 8: invalid window still 422


def test_invalid_window_still_returns_422(feature_names, normal_window, client):
    """A malformed window must still be rejected at the API boundary."""
    body = {
        "feature_names": list(feature_names),
        "window": normal_window[:29].tolist(),      # 29 timesteps, not 30
    }
    assert client.post("/score", json=body).status_code == 422


# ------------------------------------------- 9: FastAPI holds no buffering state


def test_fastapi_holds_no_buffer_state(detector, feature_names, normal_window, attack_window, scorer):
    """The API is stateless; the rolling buffer lives only on the client side."""
    app = create_app(detector=detector)
    # No buffer/window/stream state is attached to the app.
    assert not hasattr(app.state, "buffer")
    assert not hasattr(app.state, "window")
    assert not hasattr(app.state, "rolling_window")

    canonical = list(feature_names)
    attack_body = {"feature_names": canonical, "window": attack_window.tolist()}
    normal_body = {"feature_names": canonical, "window": normal_window.tolist()}

    # Scoring an attack window must not influence the next (normal) window.
    scorer(attack_body)
    after_attack = scorer(normal_body)
    fresh = scorer(normal_body)
    assert after_attack == fresh                    # no carry-over between requests
    assert after_attack["is_anomaly"] is False


# ------------------------------------ 10: artifacts unchanged during an e2e run


def test_artifacts_unchanged_during_e2e_run(feature_names, normal_window, attack_window, artifacts_dir, scorer):
    def digest():
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(artifacts_dir.iterdir())
            if p.is_file()
        }

    before = digest()
    for values, label in ((normal_window, 0), (attack_window, 1)):
        replay = SWaTReplay(_series_from_window(values, label, feature_names))
        list(run_pipeline(replay, scorer))
    assert digest() == before


# ----------------------------- §6: accelerated pacing uses the injected sleeper


def test_accelerated_pacing_uses_injected_sleeper_without_real_wait(feature_names, scorer):
    """A finite real_time_factor paces via the sleeper -- tests never really wait."""
    delays: list[float] = []
    n = 32
    replay = SWaTReplay(_synth_series(n, feature_names))  # 5 s effective sample period
    results = list(
        run_pipeline(replay, scorer, real_time_factor=60.0, sleeper=delays.append)
    )

    assert len(results) == n - 30 + 1
    # stream() waits before every record except the first: n-1 intended delays,
    # each sample_interval / factor = 5 / 60 s -- recorded, never actually slept.
    assert len(delays) == n - 1
    assert all(d == pytest.approx(5.0 / 60.0) for d in delays)


def test_no_wait_mode_records_no_delay(feature_names, scorer):
    """The default inf factor is genuine no-wait: the sleeper is never called."""
    calls: list[float] = []
    replay = SWaTReplay(_synth_series(31, feature_names))
    list(run_pipeline(replay, scorer, real_time_factor=math.inf, sleeper=calls.append))
    assert calls == []
