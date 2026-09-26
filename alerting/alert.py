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
    "PROCESS_ANOMALY_CATEGORY",
    "TELEMETRY_FAULT_CATEGORY",
    "severity_for",
    "MEDIUM_RATIO",
    "HIGH_RATIO",
]

# Alert categories. The MVP now has two distinct kinds of finding, and they must
# stay semantically separate (Phase 6B): a process-behaviour anomaly produced by
# TranAD, and a telemetry/sensor-health fault produced by the stuck-channel
# monitor. A TELEMETRY_FAULT is NOT an ML anomaly and must never be collapsed into
# one. Kept as a field on the alert, not hard-coded into the engine.
PROCESS_ANOMALY_CATEGORY = "PROCESS_ANOMALY"
TELEMETRY_FAULT_CATEGORY = "TELEMETRY_FAULT"

# Back-compat alias: Phase 4 used DEFAULT_CATEGORY for the (then only) ML category.
DEFAULT_CATEGORY = PROCESS_ANOMALY_CATEGORY

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
        Heuristic band (see :func:`severity_for`) -- NOT an ML output. For a
        TELEMETRY_FAULT this is a fixed placeholder (telemetry faults are not
        ML-severity-scored); ``category`` is what carries the meaning.
    opened_at / closed_at:
        Lifecycle timestamps (``closed_at`` is ``None`` until the alert closes).
    affected_channels:
        For a TELEMETRY_FAULT, the stuck channel(s) this incident covers (one per
        incident by construction -- see :meth:`AlertEngine.process_health`). Empty
        for a PROCESS_ANOMALY.
    reason:
        Short human-readable cause, populated for a TELEMETRY_FAULT
        (``STUCK_CHANNEL: ...``). ``None`` for a PROCESS_ANOMALY.
    staleness_seconds:
        For a TELEMETRY_FAULT, how long the channel has been frozen (latest
        observation). ``None`` for a PROCESS_ANOMALY.
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
    # Telemetry-fault fields (Phase 6B). Defaulted so a PROCESS_ANOMALY alert is
    # built exactly as before; only the telemetry track populates them.
    affected_channels: list[str] = field(default_factory=list)
    reason: str | None = None
    staleness_seconds: float | None = None

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
