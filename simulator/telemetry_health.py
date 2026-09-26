"""Telemetry / sensor-health monitoring: detect finite-but-stuck sensor channels.

Phase 6A. Sits at the **ingest boundary**, alongside
:class:`~simulator.window_buffer.RollingWindow`, and answers a question neither
TranAD nor the rolling buffer can: *is a channel technically finite yet frozen?*

    TelemetryRecord ──▶ TelemetryHealthMonitor.observe() ──▶ TelemetryHealthResult
                    └──▶ RollingWindow.push() ──▶ Window ──▶ TranAD (unchanged path)

Why here, and not in TranAD, FastAPI, or RollingWindow
------------------------------------------------------
* **Not TranAD.** The detector is stateless (one window → one decision) and must
  stay byte-for-byte the shipped model. A stuck channel is an *operational*
  fault, not an ML anomaly; conflating them would be exactly the mistake this
  phase avoids. See ``docs/provenance/tranad_swat_provenance.md`` §7 open item 4:
  "a channel frozen at a *plausible* constant still passes silently. Needs a
  stuck-channel check in the replay/ingest layer."
* **Not FastAPI.** The backend is thin and stateless by rule; it sees isolated
  ``30 × 45`` windows, never the continuous per-sample stream a staleness counter
  needs. Per-sample streaming state belongs to the caller (the same place
  ``RollingWindow`` lives), not the API.
* **Not RollingWindow.** The buffer owns the raw sample ring and must not be
  taught to interpret physics. This monitor is a *separate observer* of the same
  stream. Crucially it **never modifies, repairs, forward-fills, or suppresses**
  anything — it only emits an explicit health signal. The ML input is untouched.

The monitor imports only numpy: no torch, no FastAPI, no ``ml.src`` — consistent
with the rest of the simulator layer, and trivially testable in isolation.

What it detects, and what it deliberately does not
--------------------------------------------------
It flags a **STUCK_CHANNEL**: a *continuous* sensor whose finite value is
bit-identical for ``max_unchanged_samples`` consecutive samples. A genuinely
stuck/failed/disconnected analog channel repeats the exact same number; live
continuous telemetry carries measurement noise and effectively never does.

It does **not** monitor discrete actuator channels (``MV*`` valves, ``P*`` pumps,
``UV*``). Those legitimately hold one state indefinitely (an idle pump reports the
same value for the entire record), so a staleness check on them is a guaranteed
false positive. This is the "legitimately stable process value vs suspiciously
frozen telemetry" distinction the phase requires — see the provenance note
``docs/provenance/phase6a_telemetry_health.md`` for the measured evidence behind
both the monitored set and the default threshold.

Non-finite values (NaN / ±inf) are **not** this monitor's job — they are rejected
upstream by ``RollingWindow`` and the API schema. A non-finite sample simply
breaks a stuck run (it is never counted toward one), so it can never raise a
false STUCK_CHANNEL.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

__all__ = [
    "TelemetryHealthMonitor",
    "TelemetryHealthResult",
    "ChannelHealth",
    "TelemetryHealthError",
    "classify_feature",
    "continuous_features",
    "CONTINUOUS_PREFIXES",
    "DISCRETE_PREFIXES",
    "DEFAULT_MAX_UNCHANGED_SAMPLES",
    "STUCK_CHANNEL",
    "TELEMETRY_FAULT",
    "SENSOR_HEALTH",
]

# --- terminology (kept distinct from the ML side's PROCESS_ANOMALY) -----------
STUCK_CHANNEL = "STUCK_CHANNEL"   # one frozen continuous channel
TELEMETRY_FAULT = "TELEMETRY_FAULT"  # the result-level fault type
SENSOR_HEALTH = "SENSOR_HEALTH"   # the capability / category name

# SWaT tag families. Continuous analog sensors carry measurement noise and should
# not sit bit-identical for long; discrete actuators legitimately hold a state.
# The alphabetic prefix of a tag (e.g. "PIT" in PIT501, "P" in P501) selects the
# family — matched as the full leading letter run so "PIT" never collides with "P".
CONTINUOUS_PREFIXES = frozenset({"FIT", "LIT", "AIT", "DPIT", "PIT"})
DISCRETE_PREFIXES = frozenset({"MV", "P", "UV"})

# Default staleness threshold, in samples. At the shipped 5 s effective cadence
# (provenance §6) this is 4 hours. Chosen from measured normal SWaT behaviour:
# the longest bit-identical run of any *continuous* channel over the clean
# Attack_v0 normal data is 2,815 samples (AIT401, a near-quantised analyzer;
# FIT601 reaches 810 when its stage is idle). A default above 2,815 yields
# **zero false positives on that measured normal slice**. 2,880 = 4 h is that
# safe value with margin. It is intentionally conservative (favouring precision:
# never cry wolf on normal telemetry); shorter, more aggressive thresholds are
# supported via ``max_unchanged_samples`` with a documented, measured
# false-positive rate — see docs/provenance/phase6a_telemetry_health.md.
DEFAULT_MAX_UNCHANGED_SAMPLES = 2880

_ALPHA_PREFIX = re.compile(r"[A-Za-z]+")


class TelemetryHealthError(ValueError):
    """Raised on an invalid monitor configuration or a malformed observed record."""


def classify_feature(name: str) -> str:
    """Classify a SWaT tag as ``"continuous"``, ``"discrete"`` or ``"unknown"``.

    Uses the tag's alphabetic prefix (``PIT501`` → ``PIT``; ``P501`` → ``P``), so
    pressure transmitters are never confused with pumps. An unrecognised prefix is
    ``"unknown"`` and is left out of the default monitored set (fail safe: an
    unclassified channel is not staleness-checked rather than falsely flagged).
    """
    m = _ALPHA_PREFIX.match(name)
    prefix = m.group(0) if m else ""
    if prefix in CONTINUOUS_PREFIXES:
        return "continuous"
    if prefix in DISCRETE_PREFIXES:
        return "discrete"
    return "unknown"


def continuous_features(feature_names: Iterable[str]) -> tuple[str, ...]:
    """The continuous-sensor subset of ``feature_names``, in the given order.

    This is the default monitored set: discrete actuators and unknown tags are
    excluded because a legitimately parked actuator would false-positive.
    """
    return tuple(n for n in feature_names if classify_feature(n) == "continuous")


@dataclass(frozen=True)
class ChannelHealth:
    """One monitored channel currently flagged as stuck.

    Attributes
    ----------
    feature:
        The frozen channel's tag name.
    unchanged_samples:
        How many consecutive samples (including the current one) have carried the
        identical finite value. ``>= max_unchanged_samples`` by construction.
    staleness_seconds:
        ``(unchanged_samples - 1) * sample_interval_seconds`` — wall-clock span the
        value has been frozen, or ``None`` if no interval was supplied.
    last_value:
        The stuck value itself (the number being repeated).
    reason:
        Always :data:`STUCK_CHANNEL`.
    """

    feature: str
    unchanged_samples: int
    staleness_seconds: float | None
    last_value: float
    reason: str = STUCK_CHANNEL


@dataclass(frozen=True)
class TelemetryHealthResult:
    """The telemetry-health verdict for one observed sample.

    Explicitly distinct from an ML :class:`~ml.src.detector.DetectionResult`: this
    is a *sensor/telemetry* signal, never an attack claim. A system may see an ML
    anomaly, a telemetry fault, both, or neither — and this object stays
    independent of the detector's decision.
    """

    healthy: bool
    n_seen: int
    stuck_channels: tuple[ChannelHealth, ...]
    monitored_features: tuple[str, ...]
    max_unchanged_samples: int
    timestamp: object = None  # last observed sample's timestamp, if provided
    fault_type: str | None = None  # TELEMETRY_FAULT when unhealthy, else None
    category: str = field(default=SENSOR_HEALTH, repr=False)

    @property
    def affected_features(self) -> tuple[str, ...]:
        """Tag names of the currently-stuck channels (empty when healthy)."""
        return tuple(c.feature for c in self.stuck_channels)

    @property
    def reason(self) -> str | None:
        """A short human-readable reason, or ``None`` when healthy."""
        if self.healthy:
            return None
        return (
            f"{STUCK_CHANNEL}: {', '.join(self.affected_features)} unchanged for "
            f">= {self.max_unchanged_samples} samples"
        )


class TelemetryHealthMonitor:
    """Streaming per-channel staleness monitor. One instance per telemetry stream.

    Usage::

        monitor = TelemetryHealthMonitor(feature_names)      # 45 names, model order
        for record in source:                                # same records RollingWindow sees
            health = monitor.observe(record)
            if not health.healthy:
                ...  # surface a TELEMETRY_FAULT — distinct from any ML anomaly

    Stateful and per-instance-isolated (like :class:`AlertEngine`, and unlike the
    stateless detector): it must remember each channel's running unchanged count.
    It reads the record and returns a verdict; it never mutates the record, the
    rolling buffer, or the ML input.

    Parameters
    ----------
    feature_names:
        The full ordered feature vector each observed record carries (the model's
        45 names). Establishes the expected sample width.
    max_unchanged_samples:
        Flag a monitored channel once its finite value is bit-identical for this
        many consecutive samples. Must be ``>= 2``. Defaults to
        :data:`DEFAULT_MAX_UNCHANGED_SAMPLES`.
    monitored_features:
        Which channels to staleness-check. Defaults to the continuous-sensor
        subset (:func:`continuous_features`); every name must exist in
        ``feature_names``. Discrete actuators are excluded by default on purpose.
    sample_interval_seconds:
        Effective cadence, used only to report ``staleness_seconds``. Defaults to
        the SWaT 5 s effective sample period; pass ``None`` to omit wall-clock.
    """

    def __init__(
        self,
        feature_names: Sequence[str],
        *,
        max_unchanged_samples: int = DEFAULT_MAX_UNCHANGED_SAMPLES,
        monitored_features: Iterable[str] | None = None,
        sample_interval_seconds: float | None = 5.0,
    ) -> None:
        self._feature_names = tuple(feature_names)
        if len(self._feature_names) < 1:
            raise TelemetryHealthError("feature_names must be non-empty")
        self._n_features = len(self._feature_names)

        if int(max_unchanged_samples) != max_unchanged_samples:
            raise TelemetryHealthError("max_unchanged_samples must be an integer")
        if max_unchanged_samples < 2:
            raise TelemetryHealthError(
                f"max_unchanged_samples must be >= 2 (a single sample cannot be "
                f"'unchanged'), got {max_unchanged_samples}"
            )
        self._max_unchanged = int(max_unchanged_samples)

        if sample_interval_seconds is not None and sample_interval_seconds <= 0:
            raise TelemetryHealthError("sample_interval_seconds must be > 0 or None")
        self._interval = sample_interval_seconds

        if monitored_features is None:
            monitored = continuous_features(self._feature_names)
        else:
            monitored = tuple(monitored_features)

        if len(monitored) == 0:
            raise TelemetryHealthError(
                "no features to monitor (the default continuous-sensor set is "
                "empty for these feature_names; pass monitored_features explicitly)"
            )
        seen: set[str] = set()
        index_of = {name: i for i, name in enumerate(self._feature_names)}
        idx: list[int] = []
        ordered: list[str] = []
        for name in monitored:
            if name not in index_of:
                raise TelemetryHealthError(
                    f"monitored feature {name!r} is not in feature_names"
                )
            if name in seen:
                raise TelemetryHealthError(f"duplicate monitored feature {name!r}")
            seen.add(name)
            idx.append(index_of[name])
            ordered.append(name)
        self._monitored_idx = tuple(idx)
        self._monitored_features = tuple(ordered)

        self._counts = [0] * len(self._monitored_idx)
        self._prev = [math.nan] * len(self._monitored_idx)
        self._n_seen = 0

    # --------------------------------------------------------------- properties

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._feature_names

    @property
    def monitored_features(self) -> tuple[str, ...]:
        return self._monitored_features

    @property
    def max_unchanged_samples(self) -> int:
        return self._max_unchanged

    @property
    def n_seen(self) -> int:
        """Samples observed since construction or the last :meth:`reset`."""
        return self._n_seen

    # ------------------------------------------------------------------ observe

    def observe(self, record: object) -> TelemetryHealthResult:
        """Observe one telemetry sample; return its health verdict.

        ``record`` is duck-typed exactly like :meth:`RollingWindow.push`'s input
        (``.values``, optional ``.timestamp``). The record is only read. Each
        monitored channel's finite value is compared to its predecessor:
        bit-identical extends the unchanged run, anything else (including a
        non-finite value) resets it. A channel whose run reaches
        ``max_unchanged_samples`` is reported as a stuck channel.
        """
        values = np.asarray(getattr(record, "values"), dtype=np.float64)
        if values.shape != (self._n_features,):
            raise TelemetryHealthError(
                f"expected {self._n_features} features, got shape {values.shape}"
            )
        self._n_seen += 1
        timestamp = getattr(record, "timestamp", None)

        stuck: list[ChannelHealth] = []
        for slot, fidx in enumerate(self._monitored_idx):
            v = float(values[fidx])
            if not math.isfinite(v):
                # Non-finite is a separate fault class handled upstream; it never
                # counts toward a stuck run (so it can never false-flag STUCK).
                self._counts[slot] = 0
                self._prev[slot] = v
                continue
            if self._counts[slot] > 0 and v == self._prev[slot]:
                self._counts[slot] += 1
            else:
                self._counts[slot] = 1
            self._prev[slot] = v
            if self._counts[slot] >= self._max_unchanged:
                stuck.append(
                    ChannelHealth(
                        feature=self._feature_names[fidx],
                        unchanged_samples=self._counts[slot],
                        staleness_seconds=(
                            None if self._interval is None
                            else (self._counts[slot] - 1) * self._interval
                        ),
                        last_value=v,
                        reason=STUCK_CHANNEL,
                    )
                )

        healthy = not stuck
        return TelemetryHealthResult(
            healthy=healthy,
            n_seen=self._n_seen,
            stuck_channels=tuple(stuck),
            monitored_features=self._monitored_features,
            max_unchanged_samples=self._max_unchanged,
            timestamp=timestamp,
            fault_type=None if healthy else TELEMETRY_FAULT,
        )

    def reset(self) -> None:
        """Drop all per-channel counters. Staleness starts over from the next sample."""
        self._counts = [0] * len(self._monitored_idx)
        self._prev = [math.nan] * len(self._monitored_idx)
        self._n_seen = 0
