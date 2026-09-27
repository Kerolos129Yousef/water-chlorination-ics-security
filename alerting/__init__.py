"""Alert engine (Phase 4): detector decisions → deduplicated, lifecycle-managed alerts.

The stateful stage that turns a stream of per-window detection decisions into
operator-facing incidents::

    DetectionResult / EndToEndResult ──▶ AlertEngine ──▶ Alert (OPEN → CLOSED)

Boundary. This package imports no ``torch``, no FastAPI, no ``ml.src`` and no
``simulator`` -- it duck-types on the fields the detector already produces
(``is_anomaly`` / ``anomaly_score`` / ``threshold`` / ``top_features`` /
optional ``window_start`` / ``window_end``). Feature attribution is taken
verbatim from the detector; severity is an explicitly-labelled engineering
heuristic, not an ML prediction. Durability lives behind a small ``AlertStore``
(Phase 7 SQLite, Phase 8B PostgreSQL); the engine still owns all lifecycle logic.
"""

from __future__ import annotations

from .alert import (
    DEFAULT_CATEGORY,
    HIGH_RATIO,
    MEDIUM_RATIO,
    PROCESS_ANOMALY_CATEGORY,
    TELEMETRY_FAULT_CATEGORY,
    Alert,
    AlertStatus,
    Severity,
    severity_for,
)
from .engine import AlertEngine, ChannelHealthLike, DetectionLike, TelemetryHealthLike
from .store import (
    AlertStore,
    AlertStoreError,
    InMemoryAlertStore,
    PostgreSQLAlertStore,
    SQLiteAlertStore,
    alert_store_from_env,
)

__all__ = [
    "AlertEngine",
    "DetectionLike",
    "TelemetryHealthLike",
    "ChannelHealthLike",
    "Alert",
    "AlertStatus",
    "Severity",
    "severity_for",
    "DEFAULT_CATEGORY",
    "PROCESS_ANOMALY_CATEGORY",
    "TELEMETRY_FAULT_CATEGORY",
    "MEDIUM_RATIO",
    "HIGH_RATIO",
    # Phase 7 persistence (Phase 8B adds PostgreSQLAlertStore)
    "AlertStore",
    "InMemoryAlertStore",
    "SQLiteAlertStore",
    "PostgreSQLAlertStore",
    "AlertStoreError",
    "alert_store_from_env",
]
