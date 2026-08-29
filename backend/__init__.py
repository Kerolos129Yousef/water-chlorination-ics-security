"""FastAPI backend for the TranAD anomaly detector (Phase 2B).

A *thin* API boundary and nothing more:

    client ──▶ FastAPI (validate) ──▶ ml.src.detector.TranADDetector ──▶ DetectionResult

It owns no rolling buffer (that is :class:`simulator.window_buffer.RollingWindow`),
runs no replay, and duplicates no TranAD/preprocessing/scoring/threshold logic --
it reuses :mod:`ml.src`. Dependency direction is strictly ``backend -> ml.src``.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
