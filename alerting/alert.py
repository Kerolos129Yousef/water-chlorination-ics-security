"""Alert schema for the Phase 4 alert engine: the structured record of one anomaly.

Deliberately plain data. No FastAPI, no torch, no database, no ``ml.src`` /
``simulator`` imports -- an :class:`Alert` is a serialisable value object that the
:class:`~alerting.engine.AlertEngine` produces from a detector decision. The
feature attribution it carries is copied verbatim from the detector's own
``top_features`` output; nothing here re-derives or rescales it.

Severity
--------
:class:`Severity` is an **engineering heuristic**, not a model output. The project
has no research-validated severity classifier (see ``ml/configs/tranad_swat.yaml``:
``severity_bands_are_research_validated: false`` and
``docs/provenance/tranad_swat_provenance.md`` §5). :func:`severity_for` is a
transparent, deterministic banding of the ``anomaly_score / threshold`` ratio,
provided only so an operator view can sort alerts -- it must always be presented
as a heuristic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

__all__ = [
    "Alert",
    "AlertStatus",
    "Severity",
    "DEFAULT_CATEGORY",
    "severity_for",
    "MEDIUM_RATIO",
    "HIGH_RATIO",
]

# Default alert category. The MVP has exactly one detector and one kind of finding
# -- a process-behaviour anomaly -- so one category. Kept as a field, not hard-coded
# into the engine, so a future detector can label its alerts differently.
DEFAULT_CATEGORY = "PROCESS_ANOMALY"

# Heuristic severity band edges, expressed as multiples of the threshold. Chosen for
# transparency, NOT calibrated against labelled severity data (there is none).
MEDIUM_RATIO = 2.0
HIGH_RATIO = 10.0


class AlertStatus(str, Enum):
    """Minimal alert lifecycle. No ack/escalation/paging in this phase."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Severity(str, Enum):
    """Heuristic severity band -- see module docstring. Not an ML prediction."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def severity_for(anomaly_score: float, threshold: float) -> Severity:
    """Band the score/threshold ratio into a severity. Pure, deterministic heuristic.

    ``>= HIGH_RATIO`` x threshold → HIGH, ``>= MEDIUM_RATIO`` x → MEDIUM, else LOW.
    A non-positive threshold falls back to HIGH for any anomalous score (it cannot
    be a meaningful ratio). This is an engineering convenience, not a calibrated
    risk score.
    """
    if threshold <= 0:
        return Severity.HIGH
    ratio = anomaly_score / threshold
    if ratio >= HIGH_RATIO:
        return Severity.HIGH
    if ratio >= MEDIUM_RATIO:
        return Severity.MEDIUM
    return Severity.LOW


@dataclass
class Alert:
    """One logical anomaly, possibly spanning many consecutive windows.

    Deduplication means a single continuous anomaly is ONE ``Alert`` whose span
    (``window_start`` .. ``window_end``) and ``window_count`` grow as overlapping
    anomalous windows arrive; ``anomaly_score`` / ``top_features`` / ``severity``
    track the **peak** (most severe) window seen so far.

    Attributes
    ----------
    alert_id:
        Stable, deterministic identity (see :meth:`AlertEngine._make_id`).
    category:
        What kind of finding this is (default :data:`DEFAULT_CATEGORY`).
    status:
        OPEN while the anomaly persists, CLOSED once the stream returns to normal.
    detected_at:
        Timestamp the alert opened (the opening window's end), if known.
    window_start / window_end:
        Span of the alert: first window's start, latest window's end.
    anomaly_score / threshold:
        Peak anomaly score across the alert's windows, and the fixed threshold.
    is_anomaly:
        Always ``True`` for an emitted alert; kept so the record is self-describing.
    top_features:
        Peak window's top-N ``(feature, error)`` pairs, copied from the detector.
    window_count:
        How many anomalous windows have been merged into this alert.
    severity:
        Heuristic band (see :func:`severity_for`) -- NOT an ML output.
    opened_at / closed_at:
        Lifecycle timestamps (``closed_at`` is ``None`` until the alert closes).
    """

    alert_id: str
    category: str
    status: AlertStatus
    detected_at: str | None
    window_start: str | None
    window_end: str | None
    anomaly_score: float
    threshold: float
    is_anomaly: bool
    top_features: list[tuple[str, float]]
    window_count: int
    severity: Severity
    opened_at: str | None = None
    closed_at: str | None = None

    def to_dict(self) -> dict:
        """A JSON-friendly snapshot (enums → their string values, tuples → lists).

        A plain dict so a future transport (API / dashboard / store) can serialise
        an alert without importing this module's types. Enums are :class:`str`
        subclasses, so ``asdict`` already yields their values.
        """
        d = asdict(self)
        d["status"] = self.status.value
        d["severity"] = self.severity.value
        d["top_features"] = [[name, float(err)] for name, err in self.top_features]
        return d
