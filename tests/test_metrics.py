"""Metrics tests -- entirely synthetic, no dataset and no model.

The core assertion in this file is the labelling one: ``window_labels_any`` (the
research convention) and ``window_labels_last`` (the honest one) differ in a
specific, predictable way, and that difference always favours the model. If a
future change makes them agree, or makes them disagree somewhere other than
after an attack ends, something is wrong with the reasoning in
``docs/provenance/measured_detection_metrics.md``.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.src.metrics import (
    confusion_counts,
    point_adjust,
    precision_recall_f1,
    score_metrics,
    threshold_free_metrics,
    window_labels_any,
    window_labels_last,
)

WINDOW = 30


# ------------------------------------------------------------------ point_adjust

def test_point_adjust_marks_whole_segment_from_one_hit():
    y_true = np.array([0, 1, 1, 1, 1, 0])
    y_pred = np.array([0, 0, 0, 1, 0, 0])
    np.testing.assert_array_equal(
        point_adjust(y_true, y_pred), [0, 1, 1, 1, 1, 0]
    )


def test_point_adjust_leaves_undetected_segment_alone():
    y_true = np.array([0, 1, 1, 1, 0])
    y_pred = np.array([0, 0, 0, 0, 0])
    np.testing.assert_array_equal(point_adjust(y_true, y_pred), [0, 0, 0, 0, 0])


def test_point_adjust_never_removes_a_false_positive():
    """It only ever adds true positives -- the reason it inflates scores."""
    y_true = np.array([0, 0, 1, 1, 0, 0])
    y_pred = np.array([1, 1, 0, 1, 1, 1])   # 4 FPs, 1 TP
    adjusted = point_adjust(y_true, y_pred)
    assert adjusted.sum() >= y_pred.sum()
    # the normal-region predictions are untouched
    np.testing.assert_array_equal(adjusted[[0, 1, 4, 5]], [1, 1, 1, 1])


def test_point_adjust_handles_segment_at_index_zero():
    y_true = np.array([1, 1, 0, 0])
    y_pred = np.array([0, 1, 0, 0])
    np.testing.assert_array_equal(point_adjust(y_true, y_pred), [1, 1, 0, 0])


def test_point_adjust_handles_segment_at_tail():
    y_true = np.array([0, 0, 1, 1])
    y_pred = np.array([0, 0, 0, 1])
    np.testing.assert_array_equal(point_adjust(y_true, y_pred), [0, 0, 1, 1])


def test_point_adjust_treats_adjacent_segments_as_one_run():
    """Contiguous 1s are one run regardless of how many attacks produced them.

    Documents a real limitation: back-to-back attacks merge, which is also why
    the clean dataset shows 35 label segments for 36 launched attacks.
    """
    y_true = np.array([1, 1, 1, 1])
    y_pred = np.array([1, 0, 0, 0])
    np.testing.assert_array_equal(point_adjust(y_true, y_pred), [1, 1, 1, 1])


def test_point_adjust_does_not_mutate_input():
    y_true = np.array([0, 1, 1, 0])
    y_pred = np.array([0, 1, 0, 0])
    original = y_pred.copy()
    point_adjust(y_true, y_pred)
    np.testing.assert_array_equal(y_pred, original)


def test_point_adjust_shape_mismatch_raises():
    with pytest.raises(ValueError, match="same shape"):
        point_adjust(np.zeros(4), np.zeros(5))


# ------------------------------------------------------- the labelling divergence

def test_window_label_conventions_agree_at_attack_onset():
    """No early-warning inflation: both mark the same first attack window."""
    labels = np.zeros(200, dtype=int)
    labels[100:120] = 1
    any_ = window_labels_any(labels, WINDOW)
    last = window_labels_last(labels, WINDOW)
    assert np.flatnonzero(any_).min() == np.flatnonzero(last).min()


def test_window_label_conventions_diverge_after_attack_ends():
    """THE finding: `any` keeps asserting attack for window-1 samples too long.

    Attack spans samples [100, 119]. Window i is scored at timestep i+29.
    `last` marks windows whose scored timestep is in [100, 119].
    `any`  marks windows overlapping the attack at all -- scored timesteps
    [100, 148]. The extra 29 are post-attack recovery, where reconstruction
    error is still elevated, so they read as true positives under `any` and as
    false positives under `last`.
    """
    labels = np.zeros(200, dtype=int)
    labels[100:120] = 1
    any_ = window_labels_any(labels, WINDOW)
    last = window_labels_last(labels, WINDOW)

    differing = np.flatnonzero(any_ != last)
    assert len(differing) == WINDOW - 1                 # exactly 29

    scored_timesteps = differing + WINDOW - 1
    assert scored_timesteps.min() == 120                # first sample after the attack
    assert scored_timesteps.max() == 119 + WINDOW - 1   # 148
    assert (any_[differing] == 1).all()                 # always in the model's favour
    assert (last[differing] == 0).all()


def test_any_labelling_inflates_the_positive_class():
    labels = np.zeros(500, dtype=int)
    labels[100:120] = 1
    labels[300:340] = 1
    any_ = window_labels_any(labels, WINDOW)
    last = window_labels_last(labels, WINDOW)
    # two isolated segments -> 2 * (window - 1) extra positives
    assert any_.sum() - last.sum() == 2 * (WINDOW - 1)


def test_window_labels_last_is_a_plain_shift():
    labels = np.arange(40) % 2
    np.testing.assert_array_equal(
        window_labels_last(labels, WINDOW), labels[WINDOW - 1 :]
    )


def test_window_label_counts_match_window_count():
    labels = np.zeros(100, dtype=int)
    for fn in (window_labels_any, window_labels_last):
        assert len(fn(labels, WINDOW)) == 100 - WINDOW + 1


def test_window_labels_reject_short_input():
    for fn in (window_labels_any, window_labels_last):
        with pytest.raises(ValueError, match="at least"):
            fn(np.zeros(WINDOW - 1), WINDOW)


def test_window_labels_reject_2d():
    for fn in (window_labels_any, window_labels_last):
        with pytest.raises(ValueError, match="1-D"):
            fn(np.zeros((10, 2)), 3)


# ------------------------------------------------------------------ P/R/F1 maths

def test_precision_recall_f1_hand_computed():
    y_true = np.array([0, 0, 1, 1, 1, 0])
    y_pred = np.array([0, 1, 1, 1, 0, 0])          # tp=2 fp=1 fn=1 tn=2
    r = precision_recall_f1(y_true, y_pred)
    assert (r.true_positives, r.false_positives, r.false_negatives, r.true_negatives) == (2, 1, 1, 2)
    assert r.precision == pytest.approx(2 / 3)
    assert r.recall == pytest.approx(2 / 3)
    assert r.f1 == pytest.approx(2 / 3)
    assert r.point_adjusted is False


def test_point_adjustment_raises_f1_on_the_same_predictions():
    """Same scores, two protocols -- the adjusted one cannot be lower."""
    y_true = np.zeros(60, dtype=int)
    y_true[10:30] = 1
    y_pred = np.zeros(60, dtype=int)
    y_pred[15] = 1                                  # one hit inside the segment
    plain = precision_recall_f1(y_true, y_pred)
    adjusted = precision_recall_f1(y_true, y_pred, point_adjusted=True)
    assert plain.recall == pytest.approx(1 / 20)
    assert adjusted.recall == pytest.approx(1.0)
    assert adjusted.f1 > plain.f1


def test_zero_division_yields_zero_not_nan():
    r = precision_recall_f1(np.array([0, 0, 1]), np.array([0, 0, 0]))
    assert r.precision == 0.0 and r.f1 == 0.0


def test_false_positive_rate():
    y_true = np.array([0, 0, 0, 0, 1])
    y_pred = np.array([1, 0, 0, 0, 1])              # 1 fp out of 4 negatives
    assert precision_recall_f1(y_true, y_pred).false_positive_rate == pytest.approx(0.25)


def test_confusion_counts_partition_the_input():
    rng = np.random.default_rng(0)
    y_true = rng.integers(0, 2, 100)
    y_pred = rng.integers(0, 2, 100)
    assert sum(confusion_counts(y_true, y_pred)) == 100


# ------------------------------------------------------------------- thresholding

def test_score_metrics_uses_strictly_greater():
    """Matches the research `scores > thr` and the detector's decision rule."""
    scores = np.array([0.5, 1.0, 1.5])
    r = score_metrics(scores, np.array([0, 0, 1]), threshold=1.0)
    assert r.n_predicted_positive == 1                # the 1.0 does NOT count


def test_score_metrics_shape_mismatch_raises():
    with pytest.raises(ValueError, match="same shape"):
        score_metrics(np.zeros(10), np.zeros(9), threshold=0.5)


def test_threshold_free_metrics_reports_base_rate():
    """PR-AUC is prevalence-sensitive, so the base rate travels with it."""
    y = np.array([0] * 90 + [1] * 10)
    out = threshold_free_metrics(np.linspace(0, 1, 100), y)
    assert out["base_rate"] == pytest.approx(0.10)
    assert out["roc_auc"] == pytest.approx(1.0)


def test_threshold_free_metrics_single_class_is_nan_not_crash():
    out = threshold_free_metrics(np.random.default_rng(0).random(50), np.zeros(50))
    assert np.isnan(out["roc_auc"]) and np.isnan(out["pr_auc"])


def test_perfect_separation_gives_f1_one():
    scores = np.array([0.1, 0.2, 0.9, 0.95])
    r = score_metrics(scores, np.array([0, 0, 1, 1]), threshold=0.5)
    assert r.f1 == pytest.approx(1.0)
