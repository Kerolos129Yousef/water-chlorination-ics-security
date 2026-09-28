"""Golden-vector regression tests.

Locks the end-to-end pipeline against two windows drawn from the verified SWaT
dataset (reconstructed clean Attack_v0, provenance section 6). The fixture
carries raw inputs *and* expected outputs, so these tests need no dataset.

Regenerate with ``tests/fixtures/generate_golden_vectors.py``.

The suite is designed to FAIL if any of the following changes:

===========================  ==================================================
change                       caught by
===========================  ==================================================
feature order                test_feature_order_is_locked
                             test_feature_permutation_changes_score
flatten order (C -> F)       test_fortran_flatten_changes_score
clipping introduced          test_clipping_changes_attack_score
window size                  test_window_size_is_locked
                             test_wrong_window_size_rejected
model architecture           test_architecture_is_locked
scoring formula              test_golden_scores_match
                             test_golden_feature_errors_match
===========================  ==================================================

Tolerance note: scores are compared at ``rtol=1e-6``. Bit-exact equality would
make the suite brittle across torch builds, while a formula, ordering or
architecture change moves the score by orders of magnitude -- far outside this
band. The generating environment is recorded in the fixture so genuine
cross-version drift is diagnosable rather than mysterious.

Per-feature reconstruction errors are compared at ``rtol=1e-5`` with an absolute
floor of ``atol=1e-9`` (``FEATURE_ERROR_ATOL``). The fixtures were generated on an
Intel CPU; torch's MKL/oneDNN GEMM kernels round vendor-specifically, so on AMD
runners the near-zero ``normal`` errors drift up to ~8e-10 absolute (measured on
GitHub's AMD EPYC 7763/9V74; Phase 9 diagnostic, docs/provenance/phase9_devsecops.md).
That is below the ``atol`` floor, so genuine formula/ordering/architecture changes
(orders of magnitude) are still caught, while physically-meaningless cross-vendor
FP noise on ~1e-6-magnitude errors does not. The anomaly SCORE and decision are
unaffected (they match to ~1e-9) and stay strict.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from tests.conftest import FLAT_DIM, N_FEATURES, WINDOW

SCORE_RTOL = 1e-6

# Absolute floor for the per-feature reconstruction-error comparison. The
# relative check stays at rtol=1e-5; this floor absorbs cross-CPU-vendor FP noise
# (Intel fixtures vs AMD CI runners) on near-zero error components, measured at
# <=8e-10 (see the module docstring / Phase 9 provenance). It is far below any
# real formula/ordering/architecture change, which shifts errors by orders of
# magnitude.
FEATURE_ERROR_ATOL = 1e-9


# ------------------------------------------------------------ fixture integrity

def test_fixture_records_generating_environment(golden):
    env = golden["environment"]
    for key in ("python", "torch", "numpy"):
        assert env.get(key), f"fixture must record {key} version"


def test_fixture_source_is_the_clean_attack_v0(golden):
    src = golden["source"]
    assert src["total_rows"] == 449_919      # gap-free reconstruction
    assert src["subsample"] == 5
    assert src["window"] == WINDOW


def test_fixture_covers_both_regimes(golden_cases):
    assert set(golden_cases) == {"normal", "attack"}
    assert golden_cases["normal"]["window_label"] == 0
    assert golden_cases["attack"]["window_label"] == 1


# --------------------------------------------------------------- locked contract

def test_feature_order_is_locked(golden, feature_names):
    """Any reordering or substitution of feature_names.json fails here."""
    assert golden["contract"]["feature_names"] == feature_names


def test_window_size_is_locked(golden, detector):
    assert golden["source"]["window"] == detector.window == WINDOW
    assert golden["contract"]["n_features"] == detector.n_features == N_FEATURES


def test_threshold_is_locked(golden, detector):
    assert golden["contract"]["threshold"] == detector.threshold


def test_architecture_is_locked(artifacts_dir, golden):
    """Rebuild from the locked window/feats and require an exact strict load."""
    import torch

    from ml.src.model import TranAD

    feats = golden["contract"]["n_features"]
    window = golden["source"]["window"]
    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    model = TranAD(feats=feats, window=window)
    model.load_state_dict(saved, strict=True)   # raises on any architecture drift


# --------------------------------------------------------- preprocessing parity

@pytest.mark.parametrize("case_name", ["normal", "attack"])
def test_golden_preprocessed_hash_matches(preprocessor, golden_cases, case_name):
    """Byte-level lock on the preprocessing chain output."""
    case = golden_cases[case_name]
    raw = np.array(case["raw_window"], dtype=np.float64)
    out = preprocessor.transform(raw)
    digest = hashlib.sha256(np.ascontiguousarray(out, dtype=np.float32).tobytes()).hexdigest()
    assert digest == case["expected"]["preprocessed_sha256"]


def test_normal_window_stays_within_training_bounds(preprocessor, golden_cases):
    """A normal window scales into [0, 1] -- it lies inside the fitted MinMax range."""
    exp = golden_cases["normal"]["expected"]
    assert exp["preprocessed_min"] == pytest.approx(0.0, abs=1e-6)
    assert exp["preprocessed_max"] == pytest.approx(1.0, abs=1e-6)


def test_attack_window_escapes_training_bounds(preprocessor, golden_cases):
    """The attack window scales OUTSIDE [0, 1].

    This is the mechanism that drives reconstruction error up through the
    Sigmoid-terminated fcn, and the empirical reason clip=False matters.
    """
    raw = np.array(golden_cases["attack"]["raw_window"], dtype=np.float64)
    out = preprocessor.transform(raw)
    assert out.min() < 0.0 or out.max() > 1.0
    assert out.max() > 1.0


# ------------------------------------------------------------- inference parity

@pytest.mark.parametrize("case_name", ["normal", "attack"])
def test_golden_scores_match(detector, golden_cases, case_name):
    case = golden_cases[case_name]
    raw = np.array(case["raw_window"], dtype=np.float64)
    result = detector.score(raw)
    assert result.anomaly_score == pytest.approx(
        case["expected"]["anomaly_score"], rel=SCORE_RTOL
    )
    assert result.is_anomaly is case["expected"]["is_anomaly"]


@pytest.mark.parametrize("case_name", ["normal", "attack"])
def test_golden_feature_errors_match(detector, golden_cases, case_name):
    case = golden_cases[case_name]
    raw = np.array(case["raw_window"], dtype=np.float64)
    errors = detector.score(raw).feature_errors
    expected = np.array(case["expected"]["feature_errors"], dtype=np.float64)
    assert errors.shape == expected.shape == (N_FEATURES,)
    np.testing.assert_allclose(errors, expected, rtol=1e-5, atol=FEATURE_ERROR_ATOL)


def test_golden_decisions_straddle_the_threshold(detector, golden_cases):
    """The two cases must land on opposite sides, or the suite proves nothing."""
    normal = detector.score(np.array(golden_cases["normal"]["raw_window"], dtype=np.float64))
    attack = detector.score(np.array(golden_cases["attack"]["raw_window"], dtype=np.float64))
    assert normal.is_anomaly is False
    assert attack.is_anomaly is True
    assert attack.anomaly_score > normal.anomaly_score


# --------------------------------------------- mutations that must change results

def test_feature_permutation_changes_score(detector, golden_cases):
    """Swapping two feature columns must move the score off the golden value."""
    raw = np.array(golden_cases["normal"]["raw_window"], dtype=np.float64)
    baseline = detector.score(raw).anomaly_score
    permuted = raw.copy()
    permuted[:, [0, 1]] = permuted[:, [1, 0]]
    assert detector.score(permuted).anomaly_score != pytest.approx(baseline, rel=SCORE_RTOL)


def test_fortran_flatten_changes_score(
    detector, standard_scaler, minmax_scaler, golden_cases
):
    """Transposed flattening must not reproduce the golden score."""
    from ml.src.scoring import score_windows

    raw = np.array(golden_cases["attack"]["raw_window"], dtype=np.float64)
    z = standard_scaler.transform(raw).astype(np.float32)
    wrong = minmax_scaler.transform(z.reshape(1, FLAT_DIM, order="F")).reshape(
        1, WINDOW, N_FEATURES
    )
    wrong_score = float(score_windows(detector._model, wrong.astype(np.float32))[0][0])
    assert wrong_score != pytest.approx(
        golden_cases["attack"]["expected"]["anomaly_score"], rel=SCORE_RTOL
    )


def test_clipping_changes_attack_score(detector, preprocessor, golden_cases):
    """Clipping the scaled window to [0, 1] must change the attack score.

    Empirical proof that clip=False is load-bearing: the attack window's
    out-of-range excursion carries detection signal, and clipping discards it.
    """
    from ml.src.scoring import score_windows

    raw = np.array(golden_cases["attack"]["raw_window"], dtype=np.float64)
    prepared = preprocessor.transform(raw)
    clipped = np.clip(prepared, 0.0, 1.0)
    assert not np.array_equal(prepared, clipped), "attack window must exceed [0, 1]"

    clipped_score = float(score_windows(detector._model, clipped)[0][0])
    golden_score = golden_cases["attack"]["expected"]["anomaly_score"]
    assert clipped_score != pytest.approx(golden_score, rel=SCORE_RTOL)


@pytest.mark.parametrize("bad_window", [29, 31])
def test_wrong_window_size_rejected(detector, golden_cases, bad_window):
    """Truncating or extending the window must raise, not silently rescale."""
    from ml.src.preprocessing import PreprocessingError

    raw = np.array(golden_cases["normal"]["raw_window"], dtype=np.float64)
    if bad_window < WINDOW:
        mutated = raw[:bad_window]
    else:
        mutated = np.vstack([raw, raw[-1:]])
    with pytest.raises(PreprocessingError, match="expected window shape"):
        detector.score(mutated)
