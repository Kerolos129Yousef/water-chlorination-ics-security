"""SWaT replay + rolling-window layer (Phase 2A).

Feeds valid ``30 x 45`` telemetry windows to the existing, stateless TranAD
detector. Two independent components with a clear boundary::

    SWaTReplay ──TelemetryRecord──▶ RollingWindow ──Window(30x45 raw)──▶ TranADDetector.score()
     (owns the data +               (OWNS the 30-sample                 (ml.src.detector,
      pacing + segments)             rolling buffer)                     unchanged & stateless)

Nothing here imports FastAPI, torch, or the detector; the replay reuses the
provenance-backed clean Attack_v0 reconstruction from :mod:`ml.src.dataset`. The
Phase 3 :mod:`~simulator.pipeline` glue drives this layer through the FastAPI
``/score`` boundary via an *injected* scorer, so the same rule still holds.
"""

from __future__ import annotations

from .pipeline import (
    EndToEndResult,
    HttpScorer,
    Scorer,
    run_pipeline,
    window_to_request,
)
from .swat_replay import (
    DATASET_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_SUBSAMPLE,
    ReplayError,
    SWaTReplay,
    TelemetryRecord,
)
from .window_buffer import RollingWindow, Window, WindowBufferError

__all__ = [
    "SWaTReplay",
    "TelemetryRecord",
    "ReplayError",
    "DATASET_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_SUBSAMPLE",
    "RollingWindow",
    "Window",
    "WindowBufferError",
    # Phase 3 end-to-end integration (producer/client side)
    "run_pipeline",
    "window_to_request",
    "EndToEndResult",
    "Scorer",
    "HttpScorer",
]
