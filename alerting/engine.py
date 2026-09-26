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

import logging
from typing import Protocol, runtime_checkable

from .alert import (
    DEFAULT_CATEGORY,
    TELEMETRY_FAULT_CATEGORY,
    Alert,
    AlertStatus,
    Severity,
    severity_for,
)
from .store import AlertStore, InMemoryAlertStore

__all__ = ["AlertEngine", "DetectionLike", "TelemetryHealthLike", "ChannelHealthLike"]

logger = logging.getLogger("alerting.engine")

# Sentinel: distinguishes "caller did not pass a timestamp" (fall back to the
# result's own field) from "caller explicitly passed None".
_UNSET = object()


def _seq_from_id(alert_id: str) -> int:
    """Recover the monotonic sequence embedded in an alert id.

    Both id forms put the counter as the 2nd dash-delimited token
    (``alert-0007-...`` → 7, ``tfault-0003-FIT101-...`` → 3). Used on restart to
    resume the counters past every persisted id, so restored engines never mint a
    colliding id. Malformed ids contribute 0 (they cannot lower the max).
    """
    try:
        return int(alert_id.split("-")[1])
    except (IndexError, ValueError):
        return 0


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


@runtime_checkable
class ChannelHealthLike(Protocol):
    """One stuck channel, as produced by ``simulator.telemetry_health.ChannelHealth``.

    Duck-typed so the engine never imports ``simulator`` (boundary preserved).
    """

    feature: str
    unchanged_samples: int
    staleness_seconds: float | None


@runtime_checkable
class TelemetryHealthLike(Protocol):
    """A per-observation telemetry-health verdict.

    Satisfied by ``simulator.telemetry_health.TelemetryHealthResult`` and by the
    backend's ``TelemetryHealthPayload`` wire model -- the engine duck-types on
    both, importing neither. ``healthy`` and ``stuck_channels`` must be
    consistent (``healthy`` iff no stuck channels); an inconsistent state is
    rejected by :meth:`AlertEngine.process_health`.
    """

    healthy: bool
    stuck_channels: tuple[ChannelHealthLike, ...]


class AlertEngine:
    """Turns a stream of detector decisions into deduplicated, lifecycle-managed alerts.

    Usage::

        engine = AlertEngine(top_n=5)
        for result in detections:                 # DetectionResult or EndToEndResult
            alert = engine.process(result)        # None, or the affected Alert
        engine.alerts        # every alert opened, in order (open + closed)
        engine.active_alert  # the currently-open alert, or None

    State is per-instance and fully isolated when using the default in-memory store:
    two engines never share alerts, counters, or the open slot. Given a *durable*
    store (:class:`~alerting.store.SQLiteAlertStore`), a fresh engine on the same
    store **recovers** the active incidents, history, and id counters on
    construction (Phase 7) -- a restart resumes the exact lifecycle, minting no
    duplicate incidents or ids.
    """

    def __init__(
        self,
        top_n: int = 5,
        category: str = DEFAULT_CATEGORY,
        store: AlertStore | None = None,
    ) -> None:
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        self.top_n = top_n
        self.category = category
        # Persistence sits BEHIND the engine: the engine makes every lifecycle
        # decision, the store only makes the results durable. Default = in-memory
        # (identical to pre-Phase-7 behaviour); inject SQLiteAlertStore for durability.
        self._store: AlertStore = store if store is not None else InMemoryAlertStore()
        # Live working pointers for O(1) lifecycle decisions (also persisted). The
        # store, not this pointer set, is the source of truth for history.
        self._active: Alert | None = None
        self._seq = 0  # monotonic → deterministic ids; resumed from the store on restart
        # Telemetry-fault track (Phase 6B): one open incident per stuck channel,
        # kept entirely separate from the single ML active slot above so the two
        # categories never interfere. Keyed by channel name.
        self._telemetry_active: dict[str, Alert] = {}
        self._tseq = 0
        self._recover()

    def _recover(self) -> None:
        """Rebuild live pointers + id counters from persisted state (restart-safe).

        Idempotent and side-effect-free on the store: it only reads. Active
        incidents are re-adopted so deduplication/closure continue seamlessly; the
        counters resume past every persisted id so new incidents never collide.
        """
        for alert in self._store.query():
            seq = _seq_from_id(alert.alert_id)
            if alert.category == TELEMETRY_FAULT_CATEGORY:
                self._tseq = max(self._tseq, seq)
                if alert.status is AlertStatus.OPEN:
                    channel = alert.affected_channels[0] if alert.affected_channels else alert.alert_id
                    self._telemetry_active[channel] = alert
            else:
                self._seq = max(self._seq, seq)
                if alert.status is AlertStatus.OPEN:
                    if self._active is not None:
                        # Corruption guard: at most one ML incident may be open. Keep
                        # the most recent (open order) and leave the rest as history.
                        logger.warning(
                            "recovery found multiple open PROCESS_ANOMALY incidents; "
                            "keeping the most recent (%s)", alert.alert_id
                        )
                    self._active = alert

    # --------------------------------------------------------------- properties

    @property
    def active_alert(self) -> Alert | None:
        """The currently-open PROCESS_ANOMALY alert, or ``None`` if normal.

        ML-only, unchanged from Phase 4 -- telemetry faults are a separate track
        (:attr:`active_telemetry_faults`).
        """
        return self._active

    @property
    def active_telemetry_faults(self) -> tuple[Alert, ...]:
        """Currently-open TELEMETRY_FAULT incidents, one per stuck channel.

        Order is stable (channel first-seen order); empty when no channel is stuck.
        """
        return tuple(self._telemetry_active.values())

    @property
    def alerts(self) -> tuple[Alert, ...]:
        """Every alert opened by this engine, in open order (both categories).

        Read from the durable store, so it includes incidents recovered after a
        restart. With the default in-memory store this is the same live objects as
        before.
        """
        return tuple(self._store.query())

    @property
    def store(self) -> AlertStore:
        """The backing persistence store (for diagnostics / lifecycle management)."""
        return self._store

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

    # ------------------------------------------------------ telemetry faults (6B)

    def process_health(
        self,
        result: TelemetryHealthLike,
        *,
        timestamp: object = _UNSET,
    ) -> list[Alert]:
        """Feed one telemetry-health verdict through the fault lifecycle.

        Independent of :meth:`process` -- a telemetry fault is not an ML anomaly
        and the two tracks never touch. Behaviour:

        * a channel that has newly crossed the staleness threshold **opens** a
          TELEMETRY_FAULT (one incident per channel);
        * a channel already stuck **extends** its incident (dedup: count grows,
          staleness/span updated) -- no duplicate incident is created;
        * a channel that is no longer stuck (the deterministic recovery rule: the
          first verdict in which it is absent from ``stuck_channels``) **closes**
          its incident.

        Returns every incident affected by this observation (opened, extended, or
        closed), in a deterministic order. ``result`` is only read. Timestamps come
        from ``timestamp`` if given, else from the result's own ``timestamp``.

        Raises ``ValueError`` on a malformed or self-inconsistent verdict (e.g.
        ``healthy=True`` with stuck channels, a channel with no name, or a
        non-positive unchanged count).
        """
        ts = getattr(result, "timestamp", None) if timestamp is _UNSET else timestamp

        if not hasattr(result, "healthy"):
            raise ValueError("telemetry-health result has no 'healthy' field")
        healthy = bool(result.healthy)
        stuck = tuple(getattr(result, "stuck_channels", ()) or ())

        # Consistency guard: healthy iff there are no stuck channels. Rejecting the
        # contradiction here is what keeps a malformed state from silently opening
        # or dropping incidents.
        if healthy and stuck:
            raise ValueError(
                "inconsistent telemetry-health state: healthy=True but "
                f"{len(stuck)} stuck channel(s) present"
            )
        if not healthy and not stuck:
            raise ValueError(
                "inconsistent telemetry-health state: healthy=False but no "
                "stuck_channels"
            )

        # Validate + collect the current stuck set.
        current: dict[str, tuple[int, float | None]] = {}
        for ch in stuck:
            feature = getattr(ch, "feature", None)
            if not isinstance(feature, str) or not feature:
                raise ValueError("stuck channel is missing a non-empty 'feature'")
            count = getattr(ch, "unchanged_samples", None)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(
                    f"stuck channel {feature!r} has invalid unchanged_samples: {count!r}"
                )
            if feature in current:
                raise ValueError(f"duplicate stuck channel {feature!r}")
            staleness = getattr(ch, "staleness_seconds", None)
            current[feature] = (count, staleness)

        affected: list[Alert] = []
        # Open or extend each currently-stuck channel.
        for feature, (count, staleness) in current.items():
            existing = self._telemetry_active.get(feature)
            if existing is None:
                affected.append(self._open_telemetry(feature, count, staleness, ts))
            else:
                affected.append(self._extend_telemetry(existing, count, staleness, ts))
        # Close channels that recovered (were open, no longer stuck).
        for feature in list(self._telemetry_active):
            if feature not in current:
                affected.append(self._close_telemetry(feature, ts))
        return affected

    def reset(self) -> None:
        """Drop all state: both tracks, history, the id counters, and the store."""
        self._active = None
        self._seq = 0
        self._telemetry_active.clear()
        self._tseq = 0
        self._store.clear()

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
        # Persist first, then adopt: if the durable write fails we raise without a
        # dangling in-memory incident (open stays consistent with the store).
        self._store.upsert(alert)
        self._active = alert
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
        self._store.upsert(alert)
        return alert

    def _close_if_open(self, we) -> Alert | None:
        if self._active is None:
            return None
        alert = self._active
        alert.status = AlertStatus.CLOSED
        alert.closed_at = we if we is not None else alert.window_end
        self._store.upsert(alert)
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

    # -------------------------------------------------- telemetry transitions (6B)

    def _open_telemetry(self, feature, unchanged, staleness, ts) -> Alert:
        self._tseq += 1
        alert = Alert(
            alert_id=self._make_tid(self._tseq, feature, ts),
            category=TELEMETRY_FAULT_CATEGORY,
            status=AlertStatus.OPEN,
            detected_at=ts,
            window_start=ts,
            window_end=ts,
            # Telemetry faults are not ML-scored: these ML fields are inert here.
            anomaly_score=0.0,
            threshold=0.0,
            is_anomaly=False,
            top_features=[],
            window_count=1,
            # Fixed placeholder -- category, not severity, carries the meaning.
            severity=Severity.MEDIUM,
            opened_at=ts,
            closed_at=None,
            affected_channels=[feature],
            reason=self._stuck_reason(feature, unchanged),
            staleness_seconds=staleness,
        )
        self._store.upsert(alert)
        self._telemetry_active[feature] = alert
        return alert

    def _extend_telemetry(self, alert: Alert, unchanged, staleness, ts) -> Alert:
        """Merge another stuck observation into the open incident (dedup)."""
        alert.window_count += 1
        if ts is not None:
            alert.window_end = ts
        alert.staleness_seconds = staleness
        alert.reason = self._stuck_reason(alert.affected_channels[0], unchanged)
        self._store.upsert(alert)
        return alert

    def _close_telemetry(self, feature, ts) -> Alert:
        alert = self._telemetry_active.pop(feature)
        alert.status = AlertStatus.CLOSED
        alert.closed_at = ts if ts is not None else alert.window_end
        self._store.upsert(alert)
        return alert

    @staticmethod
    def _stuck_reason(feature: str, unchanged: int) -> str:
        return f"STUCK_CHANNEL: {feature} frozen ({unchanged} unchanged samples)"

    @staticmethod
    def _make_tid(seq: int, feature: str, ts) -> str:
        """Deterministic id for a telemetry-fault incident (separate counter)."""
        suffix = str(ts) if ts is not None else "na"
        return f"tfault-{seq:04d}-{feature}-{suffix}"
