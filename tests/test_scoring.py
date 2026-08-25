"""Scoring tests: the formula must stay exactly the research formula.

The shipped threshold was calibrated against this exact quantity, so any change
here silently invalidates ``threshold.json``.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from ml.src.scoring import (
    anomaly_scores,
    per_feature_errors,
    reconstruct,
    score_windows,
)
from tests.conftest import N_FEATURES, WINDOW


@pytest.fixture(scope="module")
def model(artifacts_dir):
    from ml.src.model import load_tranad

    return load_tranad(artifacts_dir / "model.pt", feats=N_FEATURES, window=WINDOW)


def test_reconstruct_returns_both_outputs_and_target(model):
    windows = torch.zeros(3, WINDOW, N_FEATURES)
    x1, x2, elem = reconstruct(model, windows)
    for t in (x1, x2, elem):
        assert t.shape == (1, 3, N_FEATURES)
    # The two decoders must not be degenerate duplicates of each other.
    assert not torch.equal(x1, x2)


def test_elem_is_the_last_timestep(model):
    """TranAD reconstructs the final timestep; the target must be exactly that."""
    windows = torch.arange(
        WINDOW * N_FEATURES, dtype=torch.float32
    ).reshape(1, WINDOW, N_FEATURES)
    _, _, elem = reconstruct(model, windows)
    torch.testing.assert_close(elem[0, 0], windows[0, -1])


def test_score_matches_explicit_research_formula(model):
    """Recompute the notebook expression by hand and require exact agreement."""
    rng = np.random.default_rng(1234)
    windows = torch.as_tensor(
        rng.random((4, WINDOW, N_FEATURES)), dtype=torch.float32
    )
    x1, x2, elem = reconstruct(model, windows)

    expected = (
        0.5 * ((x1 - elem) ** 2).mean(dim=2) + 0.5 * ((x2 - elem) ** 2).mean(dim=2)
    ).squeeze(0)
    torch.testing.assert_close(anomaly_scores(x1, x2, elem), expected, rtol=0, atol=0)


def test_per_feature_errors_shape_and_blend(model):
    rng = np.random.default_rng(7)
    windows = torch.as_tensor(rng.random((5, WINDOW, N_FEATURES)), dtype=torch.float32)
    x1, x2, elem = reconstruct(model, windows)

    errs = per_feature_errors(x1, x2, elem)
    assert errs.shape == (5, N_FEATURES)
    expected = (0.5 * (x1 - elem) ** 2 + 0.5 * (x2 - elem) ** 2).squeeze(0)
    torch.testing.assert_close(errs, expected, rtol=0, atol=0)


def test_score_equals_mean_of_per_feature_errors(model):
    """Algebraically identical; float summation order makes them near-equal, not equal."""
    rng = np.random.default_rng(99)
    windows = torch.as_tensor(rng.random((6, WINDOW, N_FEATURES)), dtype=torch.float32)
    x1, x2, elem = reconstruct(model, windows)
    torch.testing.assert_close(
        anomaly_scores(x1, x2, elem),
        per_feature_errors(x1, x2, elem).mean(dim=1),
        rtol=1e-5,
        atol=1e-8,
    )


def test_per_feature_errors_are_non_negative(model):
    rng = np.random.default_rng(3)
    windows = torch.as_tensor(rng.random((3, WINDOW, N_FEATURES)), dtype=torch.float32)
    x1, x2, elem = reconstruct(model, windows)
    assert float(per_feature_errors(x1, x2, elem).min()) >= 0.0


def test_score_windows_shapes(model):
    rng = np.random.default_rng(11)
    windows = rng.random((7, WINDOW, N_FEATURES)).astype(np.float32)
    scores, errors = score_windows(model, windows)
    assert scores.shape == (7,)
    assert errors.shape == (7, N_FEATURES)


def test_score_windows_accepts_single_window(model):
    scores, errors = score_windows(model, np.zeros((WINDOW, N_FEATURES), dtype=np.float32))
    assert scores.shape == (1,)
    assert errors.shape == (1, N_FEATURES)


def test_batch_size_is_deterministic_for_a_fixed_size(model):
    """A given batch size must reproduce bit-identically."""
    rng = np.random.default_rng(5)
    windows = rng.random((10, WINDOW, N_FEATURES)).astype(np.float32)
    for size in (3, 256):
        first_s, first_e = score_windows(model, windows, batch_size=size)
        again_s, again_e = score_windows(model, windows, batch_size=size)
        np.testing.assert_array_equal(first_s, again_s)
        np.testing.assert_array_equal(first_e, again_e)


def test_batching_does_not_change_results_beyond_float32_noise(model):
    """No cross-window state: batch size is a throughput knob, not a semantic one.

    Not bit-identical across batch sizes: a different batch dimension selects a
    different BLAS GEMM blocking, which changes float32 accumulation order.
    Measured drift is ~1e-06 relative on scores. Per-feature errors are compared
    with an absolute floor as well, because individual error terms are ~1e-03 and
    smaller, where relative tolerance is misleading (worst absolute drift
    observed: 2.5e-09).
    """
    rng = np.random.default_rng(5)
    windows = rng.random((10, WINDOW, N_FEATURES)).astype(np.float32)
    a_s, a_e = score_windows(model, windows, batch_size=256)
    b_s, b_e = score_windows(model, windows, batch_size=3)
    np.testing.assert_allclose(a_s, b_s, rtol=1e-5, atol=1e-12)
    np.testing.assert_allclose(a_e, b_e, rtol=1e-5, atol=1e-7)


def test_inference_is_deterministic(model):
    """eval() mode disables dropout, so repeated runs must be bit-identical."""
    rng = np.random.default_rng(42)
    windows = rng.random((4, WINDOW, N_FEATURES)).astype(np.float32)
    first_s, first_e = score_windows(model, windows)
    for _ in range(3):
        s, e = score_windows(model, windows)
        np.testing.assert_array_equal(first_s, s)
        np.testing.assert_array_equal(first_e, e)


def test_train_mode_would_be_nondeterministic(artifacts_dir):
    """Negative control: proves the determinism above comes from eval() mode."""
    from ml.src.model import TranAD

    model = TranAD(feats=N_FEATURES, window=WINDOW)
    model.load_state_dict(
        torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    )
    model.train()
    torch.manual_seed(0)
    windows = torch.rand(4, WINDOW, N_FEATURES)
    with torch.no_grad():
        a = anomaly_scores(*reconstruct(model, windows))
        b = anomaly_scores(*reconstruct(model, windows))
    assert not torch.equal(a, b), "dropout should make train() mode non-deterministic"


def test_bad_rank_raises(model):
    with pytest.raises(ValueError, match="expected"):
        score_windows(model, np.zeros((2, 2, WINDOW, N_FEATURES), dtype=np.float32))


def test_empty_batch_raises(model):
    with pytest.raises(ValueError, match="empty batch"):
        score_windows(model, np.zeros((0, WINDOW, N_FEATURES), dtype=np.float32))
