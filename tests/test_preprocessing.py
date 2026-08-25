"""Preprocessing contract tests.

Covers the three silent-failure modes called out in the provenance doc -- C-order
flattening, ``clip=False`` and the 1350-column MinMax -- plus input validation
and the documented P204/P206 pass-through hazard.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.src.preprocessing import PreprocessingError, TranADPreprocessor
from tests.conftest import FLAT_DIM, N_FEATURES, WINDOW


# --------------------------------------------------------------- artifact shape

def test_scalers_have_documented_dimensions(standard_scaler, minmax_scaler):
    assert standard_scaler.n_features_in_ == N_FEATURES
    assert standard_scaler.n_samples_seen_ == 221_936
    assert minmax_scaler.n_features_in_ == FLAT_DIM == 1350
    assert minmax_scaler.n_samples_seen_ == 221_907


def test_minmax_is_1350_dimensional_not_collapsed(minmax_scaler):
    """The 1350 columns must not be collapsed to 45."""
    for attr in ("scale_", "min_", "data_min_", "data_max_", "data_range_"):
        assert getattr(minmax_scaler, attr).shape == (FLAT_DIM,), attr


def test_minmax_clip_is_false(minmax_scaler):
    """clip=True would suppress the out-of-range excursions that signal attacks."""
    assert minmax_scaler.clip is False
    assert minmax_scaler.feature_range == (0, 1)


def test_minmax_bounds_are_shared_across_window_positions(minmax_scaler):
    """Under C-order, all 30 positions share per-feature bounds (spread exactly 0).

    This is the property that makes the flatten order verifiable from the
    artifact alone.
    """
    dmin = minmax_scaler.data_min_.reshape(WINDOW, N_FEATURES)  # C-order
    dmax = minmax_scaler.data_max_.reshape(WINDOW, N_FEATURES)
    assert np.ptp(dmin, axis=0).max() == 0.0
    assert np.ptp(dmax, axis=0).max() == 0.0


def test_transposed_reshape_does_not_share_bounds(minmax_scaler):
    """Negative control: the wrong layout has large spread, so the test above bites."""
    wrong = minmax_scaler.data_min_.reshape(N_FEATURES, WINDOW)
    assert np.ptp(wrong, axis=1).max() > 1.0


def test_preprocessor_rejects_clipping_scaler(artifacts_dir, tmp_path):
    """A clip=True scaler must be refused at construction, not used.

    Stages a copy of the artifacts into tmp_path and flips clip on the copy, so
    the real artifacts are never touched.
    """
    import copy
    import shutil

    import joblib

    staged = tmp_path / "art"
    shutil.copytree(artifacts_dir, staged)
    mm = copy.deepcopy(joblib.load(staged / "minmax_scaler.joblib"))
    mm.clip = True
    joblib.dump(mm, staged / "minmax_scaler.joblib")

    with pytest.raises(PreprocessingError, match="clip=True"):
        TranADPreprocessor(staged)


# ------------------------------------------------------------- feature ordering

def test_feature_names_order_preserved(preprocessor, feature_names):
    assert list(preprocessor.feature_names) == feature_names
    assert len(preprocessor.feature_names) == N_FEATURES


def test_order_records_maps_names_to_column_positions(preprocessor, feature_names):
    """Records in arbitrary key order must land in feature_names order."""
    values = {name: float(i) for i, name in enumerate(feature_names)}
    shuffled = dict(reversed(list(values.items())))
    ordered = preprocessor.order_records([shuffled])
    assert ordered.shape == (1, N_FEATURES)
    np.testing.assert_array_equal(ordered[0], np.arange(N_FEATURES, dtype=np.float64))


def test_missing_feature_raises(preprocessor, feature_names):
    rec = {name: 1.0 for name in feature_names}
    rec.pop("AIT201")
    with pytest.raises(PreprocessingError, match="missing"):
        preprocessor.order_records([rec])


def test_extra_feature_raises(preprocessor, feature_names):
    """An unexpected channel must not be silently dropped."""
    rec = {name: 1.0 for name in feature_names}
    rec["P202"] = 1.0  # dropped by VAR_THRESHOLD, not a model input
    with pytest.raises(PreprocessingError, match="unexpected"):
        preprocessor.order_records([rec])


def test_feature_permutation_changes_output(preprocessor, normal_window):
    """Reordered columns must produce different scaled values."""
    baseline = preprocessor.transform(normal_window)
    permuted = normal_window.copy()
    permuted[:, [0, 1]] = permuted[:, [1, 0]]
    assert not np.allclose(baseline, preprocessor.transform(permuted))


# ------------------------------------------------------------- shape validation

@pytest.mark.parametrize("shape", [(29, 45), (31, 45), (30, 44), (30, 46), (45, 30)])
def test_wrong_window_shape_raises(preprocessor, shape):
    with pytest.raises(PreprocessingError, match="expected window shape"):
        preprocessor.transform(np.ones(shape))


@pytest.mark.parametrize("ndim", [1, 4])
def test_wrong_rank_raises(preprocessor, ndim):
    shape = (WINDOW,) if ndim == 1 else (1, 1, WINDOW, N_FEATURES)
    with pytest.raises(PreprocessingError, match="expected a"):
        preprocessor.transform(np.ones(shape))


def test_empty_batch_raises(preprocessor):
    with pytest.raises(PreprocessingError, match="empty batch"):
        preprocessor.transform(np.ones((0, WINDOW, N_FEATURES)))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_input_raises_not_imputed(preprocessor, normal_window, bad):
    """Deliberate divergence from the research ffill/fillna(0) behaviour.

    Silent imputation is what froze six features and contaminated the threshold
    (provenance section 5.2), so production input must fail loudly.
    """
    w = normal_window.copy()
    w[5, 3] = bad
    with pytest.raises(PreprocessingError, match="non-finite"):
        preprocessor.transform(w)


def test_non_finite_error_names_the_feature(preprocessor, normal_window, feature_names):
    w = normal_window.copy()
    w[0, 6] = np.nan
    with pytest.raises(PreprocessingError, match=repr(feature_names[6])):
        preprocessor.transform(w)


def test_transform_records_requires_exactly_window_rows(preprocessor, feature_names):
    recs = [{n: 1.0 for n in feature_names} for _ in range(WINDOW - 1)]
    with pytest.raises(PreprocessingError, match="to fill one window"):
        preprocessor.transform_records(recs)


# ------------------------------------------------------------------- transform

def test_transform_output_shape_and_dtype(preprocessor, normal_window):
    out = preprocessor.transform(normal_window)
    assert out.shape == (1, WINDOW, N_FEATURES)
    assert out.dtype == np.float32
    assert out.flags["C_CONTIGUOUS"]


def test_transform_batch_shape(preprocessor, normal_window, attack_window):
    out = preprocessor.transform(np.stack([normal_window, attack_window]))
    assert out.shape == (2, WINDOW, N_FEATURES)


def test_transform_is_row_independent(preprocessor, normal_window, attack_window):
    """Batching must not couple windows together."""
    batch = preprocessor.transform(np.stack([normal_window, attack_window]))
    solo_n = preprocessor.transform(normal_window)[0]
    solo_a = preprocessor.transform(attack_window)[0]
    np.testing.assert_array_equal(batch[0], solo_n)
    np.testing.assert_array_equal(batch[1], solo_a)


def test_manual_chain_reproduces_transform(
    preprocessor, standard_scaler, minmax_scaler, normal_window
):
    """Independent reimplementation of the documented chain must agree exactly."""
    z = standard_scaler.transform(normal_window)                  # (30, 45) float64
    z = z.astype(np.float32)                                      # cast BEFORE minmax
    flat = z.reshape(1, FLAT_DIM)                                 # C-order
    scaled = minmax_scaler.transform(flat).reshape(1, WINDOW, N_FEATURES)
    np.testing.assert_array_equal(preprocessor.transform(normal_window), scaled)


def test_fortran_order_flatten_differs(
    preprocessor, standard_scaler, minmax_scaler, normal_window
):
    """The wrong flatten order must produce different numbers.

    Guards the highest-risk silent bug: transposed flattening raises nothing and
    quietly scales all 1350 columns by the wrong statistics.
    """
    z = standard_scaler.transform(normal_window).astype(np.float32)
    wrong = minmax_scaler.transform(
        z.reshape(1, FLAT_DIM, order="F")
    ).reshape(1, WINDOW, N_FEATURES)
    assert not np.allclose(preprocessor.transform(normal_window), wrong)


def test_float32_cast_position_matters(
    preprocessor, standard_scaler, minmax_scaler, normal_window
):
    """Casting after MinMax instead of before changes the result.

    Documents why the cast sits between the two scalers.
    """
    z = standard_scaler.transform(normal_window)                       # float64
    late = minmax_scaler.transform(z.reshape(1, FLAT_DIM)).astype(np.float32)
    early = preprocessor.transform(normal_window).reshape(1, FLAT_DIM)
    # Same to float32 tolerance but not bit-identical; parity requires the
    # documented order.
    assert not np.array_equal(early, late)
    np.testing.assert_allclose(early, late, rtol=1e-5, atol=1e-6)


# --------------------------------------------------- P204 / P206 documented hazard

def test_p204_p206_have_zero_variance_in_training(standard_scaler, feature_names):
    """Provenance section 5.3: P204's zero variance is a data-gap artifact, not physics."""
    for name in ("P204", "P206"):
        j = feature_names.index(name)
        assert standard_scaler.var_[j] == 0.0
        assert standard_scaler.scale_[j] == 1.0      # sklearn's zero-variance fallback
        assert standard_scaler.mean_[j] == 1.0


def test_p204_p206_minmax_range_is_degenerate(minmax_scaler, feature_names):
    """All 30 window positions of both features have data_range_ == 0."""
    dr = minmax_scaler.data_range_.reshape(WINDOW, N_FEATURES)
    for name in ("P204", "P206"):
        j = feature_names.index(name)
        assert np.all(dr[:, j] == 0.0)


def test_p204_p206_pass_through_unbounded(preprocessor, normal_window, feature_names):
    """Excursions on these two reach the model raw: out = raw - 1.0, unbounded.

    Pins the hazard so a future change to the chain cannot alter it unnoticed.
    """
    for name in ("P204", "P206"):
        j = feature_names.index(name)
        w = normal_window.copy()
        w[:, j] = 7.0
        out = preprocessor.transform(w)[0, :, j]
        np.testing.assert_allclose(out, np.full(WINDOW, 6.0, dtype=np.float32), rtol=0, atol=1e-6)
        assert out.max() > 1.0, "value escapes [0, 1] with no clipping"
