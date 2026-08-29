"""Detection metrics, with the research protocol and an honest alternative.

Every function here is pure: arrays in, numbers out, no I/O and no model. That
matters because the point of this module is to let one set of anomaly scores be
evaluated under several protocols, so the difference between the protocols is
visible instead of buried in a pipeline.

The two axes that decide whether a reported F1 is honest
-------------------------------------------------------

**1. Window labelling.** TranAD scores a 30-sample window by reconstructing only
its *last* timestep, so a score belongs to timestep ``i + window - 1``. There are
two ways to label it:

* :func:`window_labels_last` -- the label of the timestep actually scored.
* :func:`window_labels_any` -- 1 if *any* of the 30 samples is an attack. This is
  what the research used (``window_labels`` in
  ``research/notebooks/swat/SWaT_Anomaly_Detection.ipynb`` cell 11).

They are not interchangeable. For an attack spanning samples ``[a, b]``,
``window_labels_any`` marks every window overlapping it, i.e. scored timesteps
``a`` through ``b + window - 1``. So the two conventions **agree exactly at
attack onset** but ``any`` keeps asserting "attack" for ``window - 1`` further
samples after the attack has ended -- 29 windows, 145 s at the 5 s sample period.
Those are the post-attack recovery windows, where reconstruction error is still
elevated. Under ``any`` they score as true positives; under ``last`` they are
false positives. The inflation is small in count and concentrated exactly where
the model looks best.

**2. Point adjustment.** :func:`point_adjust` marks an entire true attack segment
detected if the model flagged any single point in it. It only ever converts false
negatives into true positives and never removes a false positive, so it raises
recall monotonically and can only raise precision. It is conventional in the SWaT
literature and it is what the research used (``POINT_ADJUST: True``), but Kim et
al. (2022) show it systematically overstates performance -- a random detector can
score highly under it.

Reporting guidance
------------------
Quote ``point_adjust=False`` with :func:`window_labels_last` as the deployment
number, and state the protocol whenever quoting anything else. See
``docs/provenance/measured_detection_metrics.md`` for the measured comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

__all__ = [
    "MetricResult",
    "point_adjust",
    "window_labels_any",
    "window_labels_last",
    "precision_recall_f1",
    "score_metrics",
    "threshold_free_metrics",
    "confusion_counts",
]


@dataclass(frozen=True)
class MetricResult:
    """Metrics for one (scores, labels, protocol) combination."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    n_predicted_positive: int
    n_actual_positive: int
    point_adjusted: bool

    @property
    def false_positive_rate(self) -> float:
        """FP / (FP + TN) -- the quantity a 99th-percentile threshold targets at 1%."""
        denom = self.false_positives + self.true_negatives
        return float(self.false_positives / denom) if denom else 0.0


def point_adjust(y_true: np.ndarray, y_pred: np.ndarray) -> np.ndarray:
    """Mark a whole true attack segment detected if any point in it was flagged.

    Ported unchanged from ``SWaT_Anomaly_Detection.ipynb`` cell 13 so that
    reproducing the published ``F1_fixed`` is a genuine reproduction. Kept
    verbatim rather than vectorised for exactly that reason.
    """
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred).copy()
    if y_true.shape != y_pred.shape:
        raise ValueError(
            f"y_true and y_pred must have the same shape, got "
            f"{y_true.shape} and {y_pred.shape}"
        )
    i = 0
    n = len(y_true)
    while i < n:
        if y_true[i] == 1:
            j = i
            while j < n and y_true[j] == 1:
                j += 1
            if y_pred[i:j].any():
                y_pred[i:j] = 1
            i = j
        else:
            i += 1
    return y_pred


def window_labels_any(labels: np.ndarray, window: int, stride: int = 1) -> np.ndarray:
    """The **research** convention: 1 if any sample in the window is an attack.

    Reproduces ``window_labels`` from notebook cell 11. Read the module docstring
    before reporting anything computed with this -- it extends every attack label
    ``window - 1`` samples past the attack's end.
    """
    labels = _check_labels(labels, window)
    starts = range(0, len(labels) - window + 1, stride)
    return np.array(
        [1 if labels[i : i + window].max() > 0 else 0 for i in starts], dtype=np.int8
    )


def window_labels_last(labels: np.ndarray, window: int, stride: int = 1) -> np.ndarray:
    """The **honest** convention: the label of the timestep TranAD actually scored.

    TranAD reconstructs ``src[-1:]``, the final timestep of the window, so window
    ``i`` is a statement about sample ``i + window - 1``.
    """
    labels = _check_labels(labels, window)
    starts = range(0, len(labels) - window + 1, stride)
    return np.array([int(labels[i + window - 1]) for i in starts], dtype=np.int8)


def _check_labels(labels: np.ndarray, window: int) -> np.ndarray:
    labels = np.asarray(labels)
    if labels.ndim != 1:
        raise ValueError(f"labels must be 1-D, got shape {labels.shape}")
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    if len(labels) < window:
        raise ValueError(
            f"need at least {window} labels to form one window, got {len(labels)}"
        )
    return labels


def confusion_counts(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[int, int, int, int]:
    """``(tp, fp, fn, tn)`` for binary 0/1 arrays."""
    y_true = np.asarray(y_true).astype(bool)
    y_pred = np.asarray(y_pred).astype(bool)
    tp = int(np.count_nonzero(y_true & y_pred))
    fp = int(np.count_nonzero(~y_true & y_pred))
    fn = int(np.count_nonzero(y_true & ~y_pred))
    tn = int(np.count_nonzero(~y_true & ~y_pred))
    return tp, fp, fn, tn


def precision_recall_f1(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    point_adjusted: bool = False,
) -> MetricResult:
    """Binary precision/recall/F1, optionally under the point-adjust protocol.

    Zero-division yields 0.0 for the affected metric, matching sklearn's
    ``zero_division=0`` as used in the research ``_prf``.

    The confusion counts returned are those of the **adjusted** predictions when
    ``point_adjusted=True``, since those are what produced the reported numbers.
    """
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    if y_true.shape != y_pred.shape:
        raise ValueError(
            f"y_true and y_pred must have the same shape, got "
            f"{y_true.shape} and {y_pred.shape}"
        )

    effective = point_adjust(y_true, y_pred) if point_adjusted else y_pred
    tp, fp, fn, tn = confusion_counts(y_true, effective)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return MetricResult(
        precision=float(precision),
        recall=float(recall),
        f1=float(f1),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        n_predicted_positive=int(np.count_nonzero(effective)),
        n_actual_positive=int(np.count_nonzero(y_true)),
        point_adjusted=point_adjusted,
    )


def score_metrics(
    scores: np.ndarray,
    y_true: np.ndarray,
    threshold: float,
    *,
    point_adjusted: bool = False,
) -> MetricResult:
    """Threshold a score array and evaluate it.

    Uses ``scores > threshold``, strictly greater, matching both the research
    ``(scores_test > thr_fixed)`` and
    :class:`~ml.src.detector.TranADDetector`'s decision rule. An off-by-one on
    the comparison operator would shift every metric.
    """
    scores = np.asarray(scores)
    y_true = np.asarray(y_true)
    if scores.shape != y_true.shape:
        raise ValueError(
            f"scores and y_true must have the same shape, got "
            f"{scores.shape} and {y_true.shape}. A mismatch here usually means "
            f"the window labelling dropped a different number of samples than "
            f"the windowing did."
        )
    return precision_recall_f1(
        y_true, (scores > threshold).astype(np.int8), point_adjusted=point_adjusted
    )


def threshold_free_metrics(scores: np.ndarray, y_true: np.ndarray) -> dict[str, float]:
    """ROC-AUC and PR-AUC -- no threshold, but PR-AUC is prevalence-sensitive.

    The research reported PR-AUC 0.8216 on data whose attack ratio was deflated to
    3.79% by 991,800 duplicated normal rows. On the clean Attack_v0 the ratio is
    the true 12.1402%, so the two numbers are not comparable even though both are
    "PR-AUC". ``base_rate`` is returned to make that explicit.
    """
    scores = np.asarray(scores, dtype=np.float64)
    y_true = np.asarray(y_true)
    out = {"base_rate": float(np.mean(y_true))}
    if len(np.unique(y_true)) < 2:
        out["roc_auc"] = float("nan")
        out["pr_auc"] = float("nan")
        return out
    out["roc_auc"] = float(roc_auc_score(y_true, scores))
    out["pr_auc"] = float(average_precision_score(y_true, scores))
    return out
