"""Alert engine: detector decisions in, deduplicated alert lifecycle out.

Phase 4. The one **stateful** component in the detection path -- and
deliberately so. ``TranADDetector`` is stateless (one window → one decision) and
``RollingWindow`` owns only the raw sample buffer; neither knows that fifty
consecutive anomalous windows are *one* incident. That lifecycle state lives
here, and nowhere else.

    DetectionResult / EndToEndResult ──▶ AlertEngine.process() ──▶ Alert (OPEN → CLOSED)

Consumes anything the detector already produces -- it duck-types on
``is_anomaly`` / ``anomaly_score`` / ``threshold`` / ``top_features(n)`` and the
optional ``window_start`` / ``window_end`` -- so both
:class:`ml.src.detector.DetectionResult` and
:class:`simulator.pipeline.EndToEndResult` work unchanged. It therefore imports
neither: no torch, no FastAPI, no ``ml.src``, no ``simulator``. Attribution is
taken straight from ``result.top_features``; it is never recomputed.

Deduplication / lifecycle
-------------------------
A tiny, explainable state machine over a single "currently-open" slot::

    normal            -> (idle)
    anomaly           -> OPEN a new alert            (no active alert)
    anomaly, anomaly  -> extend the SAME alert       (dedup: span/count grow, peak tracked)
    normal            -> CLOSE the active alert
    anomaly           -> OPEN a new, independent alert

No time-window buffering, no event-processing framework -- just "is there an open
alert right now?".
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .alert import DEFAULT_CATEGORY, Alert, AlertStatus, severity_for

__all__ = ["AlertEngine", "DetectionLike"]

# Sentinel: distinguishes "caller did not pass a timestamp" (fall back to the
# result's own field) from "caller explicitly passed None".
_UNSET = object()


@runtime_checkable
class DetectionLike(Protocol):
    """The minimal surface the engine needs from a detection result.

    Satisfied by both ``DetectionResult`` and ``EndToEndResult`` already; nothing
    new has to be built to feed the engine.
    """

    is_anomaly: bool
    anomaly_score: float
    threshold: float

    def top_features(self, n: int) -> list[tuple[str, float]]: ...


class AlertEngine:
    """Turns a stream of detector decisions into deduplicated, lifecycle-managed alerts.

    Usage::

        engine = AlertEngine(top_n=5)
        for result in detections:                 # DetectionResult or EndToEndResult
            alert = engine.process(result)        # None, or the affected Alert
        engine.alerts        # every alert opened, in order (open + closed)
        engine.active_alert  # the currently-open alert, or None

    State is per-instance and fully isolated: two engines never share alerts,
    counters, or the open slot.
    """

    def __init__(self, top_n: int = 5, category: str = DEFAULT_CATEGORY) -> None:
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        self.top_n = top_n
        self.category = category
        self._active: Alert | None = None
        self._alerts: list[Alert] = []
        self._seq = 0  # per-instance, monotonic → deterministic ids for a given input

    # --------------------------------------------------------------- properties

    @property
    def active_alert(self) -> Alert | None:
        """The currently-open alert, or ``None`` if the stream is normal."""
        return self._active

    @property
    def alerts(self) -> tuple[Alert, ...]:
        """Every alert opened by this engine, in open order (open and closed)."""
        return tuple(self._alerts)

    # ------------------------------------------------------------------ process

    def process(
        self,
        result: DetectionLike,
        *,
        window_start: object = _UNSET,
        window_end: object = _UNSET,
    ) -> Alert | None:
        """Feed one detection decision through the lifecycle.

        Returns the affected :class:`Alert` -- newly opened, extended, or just
        closed -- or ``None`` when a normal result arrives with no alert open.
        The ``result`` is only read (attributes + ``top_features``); it is never
        mutated. Timestamps come from the ``window_*`` kwargs if given, else from
        the result's own ``window_start`` / ``window_end`` if it has them.
        """
        ws = getattr(result, "window_start", None) if window_start is _UNSET else window_start
        we = getattr(result, "window_end", None) if window_end is _UNSET else window_end

        if not bool(result.is_anomaly):
            return self._close_if_open(we)

        score = float(result.anomaly_score)
        threshold = float(result.threshold)
        # Copy attribution out verbatim -- the detector's numbers, not ours.
        top = [(str(name), float(err)) for name, err in result.top_features(self.top_n)]

        if self._active is None:
            return self._open(ws, we, score, threshold, top)
        return self._extend(self._active, we, score, threshold, top)

    def reset(self) -> None:
        """Drop all state: active alert, history, and the id counter."""
        self._active = None
        self._alerts.clear()
        self._seq = 0

    # ------------------------------------------------------------- transitions

    def _open(self, ws, we, score, threshold, top) -> Alert:
        self._seq += 1
        alert = Alert(
            alert_id=self._make_id(self._seq, we),
            category=self.category,
            status=AlertStatus.OPEN,
            detected_at=we if we is not None else ws,
            window_start=ws,
            window_end=we,
            anomaly_score=score,
            threshold=threshold,
            is_anomaly=True,
            top_features=top,
            window_count=1,
            severity=severity_for(score, threshold),
            opened_at=we if we is not None else ws,
            closed_at=None,
        )
        self._active = alert
        self._alerts.append(alert)
        return alert

    def _extend(self, alert: Alert, we, score, threshold, top) -> Alert:
        """Merge another anomalous window into the open alert (dedup)."""
        alert.window_count += 1
        if we is not None:
            alert.window_end = we
        # Track the peak window: it drives the reported score, attribution, severity.
        if score > alert.anomaly_score:
            alert.anomaly_score = score
            alert.top_features = top
            alert.severity = severity_for(score, threshold)
        return alert

    def _close_if_open(self, we) -> Alert | None:
        if self._active is None:
            return None
        alert = self._active
        alert.status = AlertStatus.CLOSED
        alert.closed_at = we if we is not None else alert.window_end
        self._active = None
        return alert

    @staticmethod
    def _make_id(seq: int, window_end) -> str:
        """Deterministic, stable alert id.

        Same input sequence → same ids (the per-instance counter is deterministic),
        so ids are testable and reproducible. The opening window's end is appended
        purely for human readability; the counter alone guarantees uniqueness.
        """
        suffix = str(window_end) if window_end is not None else "na"
        return f"alert-{seq:04d}-{suffix}"
