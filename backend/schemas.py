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


class ScoreRequest(BaseModel):
    """One complete ``window x features`` telemetry window to score.

    The caller owns the rolling buffer (Phase 2A ``RollingWindow``); this request
    is a single fully-formed window. No streaming state is kept between requests.
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
