"""Alert engine (Phase 4): detector decisions → deduplicated, lifecycle-managed alerts.

The stateful stage that turns a stream of per-window detection decisions into
operator-facing incidents::

    DetectionResult / EndToEndResult ──▶ AlertEngine ──▶ Alert (OPEN → CLOSED)

Boundary. This package imports no ``torch``, no FastAPI, no ``ml.src`` and no
``simulator`` -- it duck-types on the fields the detector already produces
(``is_anomaly`` / ``anomaly_score`` / ``threshold`` / ``top_features`` /
optional ``window_start`` / ``window_end``). Feature attribution is taken
verbatim from the detector; severity is an explicitly-labelled engineering
heuristic, not an ML prediction. In-memory only -- no database in this phase.
"""

from __future__ import annotations

from .alert import (
    DEFAULT_CATEGORY,
    HIGH_RATIO,
    MEDIUM_RATIO,
    Alert,
    AlertStatus,
    Severity,
    severity_for,
)
from .engine import AlertEngine, DetectionLike

__all__ = [
    "AlertEngine",
    "DetectionLike",
    "Alert",
    "AlertStatus",
    "Severity",
    "severity_for",
    "DEFAULT_CATEGORY",
    "MEDIUM_RATIO",
    "HIGH_RATIO",
]
