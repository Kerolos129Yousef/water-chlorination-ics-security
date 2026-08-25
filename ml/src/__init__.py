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

from .detector import THRESHOLD_CAVEAT, DetectionResult, TranADDetector
from .model import PositionalEncoding, TranAD, load_tranad
from .preprocessing import PreprocessingError, TranADPreprocessor
from .scoring import (
    anomaly_scores,
    per_feature_errors,
    reconstruct,
    score_windows,
)

__all__ = [
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
]
