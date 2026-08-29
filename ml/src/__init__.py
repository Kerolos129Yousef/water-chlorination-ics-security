"""Production TranAD inference library for the SWaT chlorination MVP.

Pure inference: no web framework, no database, no UI, no training. Wraps the
existing verified artifacts in ``ml/artifacts/swat_TranAD/``, which are treated
as immutable.

The model, preprocessing chain and threshold semantics are pinned in
``ml/configs/tranad_swat.yaml`` and evidenced in
``docs/provenance/tranad_swat_provenance.md``.

Note the threshold calibration caveat before reporting any detection metric:
see :data:`detector.THRESHOLD_CAVEAT`.
"""

from __future__ import annotations

from .dataset import (
    DatasetError,
    SWaTSeries,
    attack_segments,
    default_dataset_dir,
    load_attack_v0,
    load_research_concat,
    sliding_windows,
)
from .detector import THRESHOLD_CAVEAT, DetectionResult, TranADDetector
from .metrics import (
    MetricResult,
    point_adjust,
    precision_recall_f1,
    score_metrics,
    threshold_free_metrics,
    window_labels_any,
    window_labels_last,
)
from .model import PositionalEncoding, TranAD, load_tranad
from .preprocessing import PreprocessingError, TranADPreprocessor
from .scoring import (
    anomaly_scores,
    per_feature_errors,
    reconstruct,
    score_windows,
)

__all__ = [
    # inference
    "TranADDetector",
    "DetectionResult",
    "THRESHOLD_CAVEAT",
    "TranAD",
    "PositionalEncoding",
    "load_tranad",
    "TranADPreprocessor",
    "PreprocessingError",
    "reconstruct",
    "anomaly_scores",
    "per_feature_errors",
    "score_windows",
    # dataset
    "SWaTSeries",
    "DatasetError",
    "load_attack_v0",
    "load_research_concat",
    "attack_segments",
    "sliding_windows",
    "default_dataset_dir",
    # metrics
    "MetricResult",
    "point_adjust",
    "window_labels_any",
    "window_labels_last",
    "precision_recall_f1",
    "score_metrics",
    "threshold_free_metrics",
]
