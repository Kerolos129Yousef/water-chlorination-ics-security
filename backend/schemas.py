"""Pydantic request/response models for the scoring API.

These enforce the *shape* contract (30 timesteps, one value per named feature,
finite, no duplicate names) and return HTTP 422 on any violation. Feature
*identity and ordering* against the loaded model's canonical
``feature_names.json`` is checked in :mod:`backend.app`, where the detector (the
source of truth for the order) is available -- keeping these models free of any
dependency on artifacts or torch.

Design note: the window is ``feature_names + window`` (a name list plus a 30xN
matrix) rather than a list of ``{name: value}`` dicts. A dict body cannot carry
duplicate keys -- JSON parsers silently collapse them -- so it could never satisfy
the "reject duplicate feature names" requirement. A parallel name list can.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

WINDOW = 30


class ChannelHealthModel(BaseModel):
    """Wire form of one stuck channel (``simulator.telemetry_health.ChannelHealth``)."""

    feature: str
    unchanged_samples: int = Field(..., ge=1)
    staleness_seconds: float | None = None
    last_value: float | None = None
    reason: str = "STUCK_CHANNEL"


class TelemetryHealthPayload(BaseModel):
    """A per-window telemetry-health verdict, computed client-side and reported here.

    The stuck-channel monitor is a per-sample *stream* observer and lives at the
    ingest layer (Phase 6A), never in the stateless API. The client reports only
    the resulting verdict for the window; the backend forwards it to the stateful
    :class:`~alerting.engine.AlertEngine` (exactly as it already forwards the
    detection decision). No per-sample state is kept in FastAPI.

    ``healthy`` must be consistent with ``stuck_channels`` (healthy iff empty); the
    AlertEngine rejects an inconsistent verdict.
    """

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    fault_type: str | None = None
    reason: str | None = None
    timestamp: str | None = None
    stuck_channels: list[ChannelHealthModel] = Field(default_factory=list)


class ScoreRequest(BaseModel):
    """One complete ``window x features`` telemetry window to score.

    The caller owns the rolling buffer (Phase 2A ``RollingWindow``); this request
    is a single fully-formed window. No streaming state is kept between requests.

    ``telemetry_health`` is optional (Phase 6B): when the caller runs the
    stuck-channel monitor it may attach the window's health verdict, which the
    backend forwards to the alert engine. It never affects scoring.
    """

    model_config = ConfigDict(extra="forbid")  # reject unknown top-level fields

    feature_names: list[str] = Field(
        ...,
        description="Feature names, one per column of `window`. Must equal the "
        "model's canonical feature_names.json exactly (same names, same order).",
    )
    window: list[list[float]] = Field(
        ...,
        description="Exactly 30 timesteps, each a list of one value per feature "
        "(aligned to `feature_names`).",
    )
    window_start: datetime | None = Field(
        None, description="Optional timestamp of the window's first sample (echoed back)."
    )
    window_end: datetime | None = Field(
        None,
        description="Optional timestamp of the window's last (scored) sample "
        "(echoed back).",
    )
    telemetry_health: TelemetryHealthPayload | None = Field(
        None,
        description="Optional client-computed stuck-channel verdict for this "
        "window; forwarded to the alert engine, never used for scoring.",
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> "ScoreRequest":
        names = self.feature_names
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate feature names: {dupes}")

        if len(self.window) != WINDOW:
            raise ValueError(
                f"window must contain exactly {WINDOW} timesteps, got {len(self.window)}"
            )

        n_features = len(names)
        for i, row in enumerate(self.window):
            if len(row) != n_features:
                raise ValueError(
                    f"timestep {i} has {len(row)} values but there are "
                    f"{n_features} feature_names; each timestep needs one value "
                    f"per feature"
                )
            for j, value in enumerate(row):
                if not math.isfinite(value):
                    raise ValueError(
                        f"non-finite value at timestep {i}, feature "
                        f"{names[j]!r}: a stuck or absent channel is an "
                        f"operational fault and is not scored"
                    )
        return self


class FeatureError(BaseModel):
    """Per-feature reconstruction error -- which sensor drove the score."""

    feature: str
    error: float


class ScoreResponse(BaseModel):
    """The detector's decision for one window, without any torch/internal detail."""

    anomaly_score: float
    threshold: float
    is_anomaly: bool
    feature_errors: list[FeatureError] = Field(
        ..., description="Per-feature contribution, canonical feature order."
    )
    feature_names: list[str] = Field(
        ..., description="Canonical feature order (echoed for self-description)."
    )
    window_start: datetime | None = None
    window_end: datetime | None = None


class HealthResponse(BaseModel):
    """Liveness + detector diagnostics. No secrets, no filesystem paths.

    On a healthy service (HTTP 200) every field is populated. If the detector
    cannot be loaded the endpoint returns HTTP 503 with ``status="unavailable"``,
    ``detector_loaded=False`` and the detector-derived diagnostics left ``null``
    -- the service reports the outage honestly instead of fabricating model
    metadata. The detector-derived fields are therefore optional.
    """

    status: str
    detector_loaded: bool
    model_type: str
    window: int | None = None
    n_features: int | None = None
    threshold: float | None = None
    threshold_metadata: dict | None = None
    threshold_caveat: str | None = None
    detail: str | None = Field(
        None, description="Human-readable reason when degraded (no paths/tracebacks)."
    )


# ------------------------------------------------------------- monitoring (Phase 5A)


class AlertModel(BaseModel):
    """Stable DTO for one alert -- the wire form of :class:`alerting.alert.Alert`.

    Built from ``Alert`` (never the raw object) so the API contract is decoupled
    from the engine's internals. ``severity`` is an engineering heuristic, not an
    ML output; ``status`` is ``OPEN`` or ``CLOSED``.
    """

    alert_id: str
    category: str = Field(
        ..., description="PROCESS_ANOMALY (TranAD) or TELEMETRY_FAULT (stuck channel)."
    )
    status: str
    severity: str
    is_anomaly: bool
    anomaly_score: float
    threshold: float
    detected_at: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    window_count: int
    top_features: list[FeatureError] = Field(
        ..., description="Peak window's top-N per-feature contributions (from the detector)."
    )
    # Telemetry-fault fields (Phase 6B): populated only for TELEMETRY_FAULT alerts.
    affected_channels: list[str] = Field(
        default_factory=list,
        description="Stuck channel(s) for a TELEMETRY_FAULT; empty for PROCESS_ANOMALY.",
    )
    reason: str | None = Field(
        None, description="Human-readable cause for a TELEMETRY_FAULT (e.g. STUCK_CHANNEL: ...)."
    )
    staleness_seconds: float | None = Field(
        None, description="How long the channel has been frozen (TELEMETRY_FAULT only)."
    )


class ActiveAlertResponse(BaseModel):
    """The currently-open alert, or ``null`` when the stream is normal."""

    active_alert: AlertModel | None = None


class StatusResponse(BaseModel):
    """System + detector + latest-detection + alert rollup for the operator view.

    Returns HTTP 200 even when the detector cannot load (``status="degraded"``,
    ``detector_loaded=False``) so the dashboard can render a degraded banner
    rather than a dead page; ``/health`` remains the 503 liveness probe.
    ``alert_state_in_memory`` reports the persistence mode truthfully: the
    in-memory store resets on restart, while SQLite/PostgreSQL recover state.
    """

    status: str
    detector_loaded: bool
    model_type: str
    window: int | None = None
    n_features: int | None = None
    threshold: float | None = None
    threshold_caveat: str | None = None
    # Most recent window scored via /score (null until the first call).
    last_anomaly_score: float | None = None
    last_is_anomaly: bool | None = None
    last_window_end: str | None = None
    # Alert rollup. active/total counts span BOTH categories; the telemetry rollup
    # is broken out so an operator view can distinguish process anomalies from
    # telemetry faults.
    active_alert_count: int = 0
    total_alert_count: int = 0
    active_telemetry_fault_count: int = Field(
        0, description="Currently-open TELEMETRY_FAULT incidents (one per stuck channel)."
    )
    telemetry_fault_channels: list[str] = Field(
        default_factory=list,
        description="Channels currently flagged as stuck (empty when telemetry is healthy).",
    )
    alert_state_in_memory: bool = Field(
        True,
        description="True only when the in-memory store is in use (state lost on "
        "restart); False when a durable backend (SQLite/PostgreSQL) is configured.",
    )
