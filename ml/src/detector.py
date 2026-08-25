"""Detector facade: artifacts in, detection decisions out.

Deliberately free of FastAPI, database, CLI and UI concerns. It holds no
buffering or streaming state either -- the caller owns the 30-sample ring
buffer. That keeps this layer trivially testable and lets a future service
scale horizontally without shared state.

Usage::

    detector = TranADDetector.from_artifacts("ml/artifacts/swat_TranAD")
    result = detector.score(window)          # window: (30, 45) raw telemetry
    result.is_anomaly, result.anomaly_score, result.feature_errors

Threshold caveat
----------------
``threshold.json`` was calibrated on a validation split in which six of the 45
model features were frozen constants (provenance §5.2-5.3). The nominal 1%
false-positive rate will not hold on complete telemetry. The threshold is
shipped unmodified by decision; :attr:`TranADDetector.threshold_caveat` carries
the warning so callers can surface it rather than rediscover it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import torch

from .model import load_tranad
from .preprocessing import TranADPreprocessor

__all__ = ["TranADDetector", "DetectionResult", "THRESHOLD_CAVEAT"]

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "swat_TranAD"

THRESHOLD_CAVEAT = (
    "threshold.json is the 99th percentile of TranAD scores on a normal-validation "
    "split in which 6 of 45 model features (MV101, AIT201, MV201, P201, P204, MV303) "
    "were frozen constants due to an upstream column-name mismatch. Expect the real "
    "false-positive rate to exceed the nominal 1%. Reference performance at this "
    "threshold is F1_fixed=0.7934 (point-adjusted); point-wise F1 is unmeasured. "
    "See docs/provenance/tranad_swat_provenance.md section 5."
)


@dataclass(frozen=True)
class DetectionResult:
    """One detection decision for one window.

    Attributes
    ----------
    anomaly_score:
        TranAD reconstruction score. Same quantity the threshold was calibrated
        on; not a probability and not normalised to [0, 1].
    threshold:
        The fixed threshold from ``threshold.json``.
    is_anomaly:
        ``anomaly_score > threshold``. Strictly greater, matching the research
        code (``scores > thr``).
    feature_errors:
        ``(45,)`` per-feature reconstruction error, aligned with
        :attr:`feature_names`. Identifies which sensors drove the score.
    feature_names:
        Feature order, carried alongside so a result is interpretable on its own.
    """

    anomaly_score: float
    threshold: float
    is_anomaly: bool
    feature_errors: np.ndarray
    feature_names: tuple[str, ...] = field(repr=False)

    def top_features(self, n: int = 5) -> list[tuple[str, float]]:
        """The ``n`` largest per-feature errors, descending."""
        order = np.argsort(self.feature_errors)[::-1][:n]
        return [(self.feature_names[i], float(self.feature_errors[i])) for i in order]


class TranADDetector:
    """Loads the TranAD artifacts and scores raw telemetry windows."""

    def __init__(
        self,
        model: torch.nn.Module,
        preprocessor: TranADPreprocessor,
        threshold: float,
        threshold_metadata: Mapping[str, object] | None = None,
        device: str | torch.device = "cpu",
    ) -> None:
        self._model = model
        self._pre = preprocessor
        self._threshold = float(threshold)
        self._threshold_metadata = dict(threshold_metadata or {})
        self._device = device

    # ------------------------------------------------------------- construction

    @classmethod
    def from_artifacts(
        cls,
        artifacts_dir: str | Path = DEFAULT_ARTIFACTS_DIR,
        window: int = 30,
        n_features: int = 45,
        device: str | torch.device = "cpu",
    ) -> "TranADDetector":
        """Build a detector from an artifact directory. Artifacts are read-only.

        ``window`` and ``n_features`` default to the verified SWaT values and are
        cross-checked against every artifact, so a mismatch fails at construction
        rather than silently producing wrong scores.
        """
        artifacts_dir = Path(artifacts_dir)
        pre = TranADPreprocessor(artifacts_dir, window=window, n_features=n_features)

        threshold_path = artifacts_dir / "threshold.json"
        if not threshold_path.is_file():
            raise FileNotFoundError(f"Required artifact missing: {threshold_path}")
        with threshold_path.open() as fh:
            meta = json.load(fh)
        if "threshold" not in meta:
            raise ValueError(f"{threshold_path} has no 'threshold' key")

        # threshold.json records the window it was calibrated for; a mismatch
        # means the threshold does not describe this pipeline.
        recorded_window = meta.get("window")
        if recorded_window is not None and int(recorded_window) != window:
            raise ValueError(
                f"threshold.json was calibrated for window={recorded_window} "
                f"but detector was constructed with window={window}"
            )

        model = load_tranad(
            artifacts_dir / "model.pt", feats=n_features, window=window, device=device
        )
        return cls(model, pre, float(meta["threshold"]), meta, device=device)

    # --------------------------------------------------------------- properties

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def threshold_metadata(self) -> dict:
        """Raw contents of ``threshold.json`` (percentile, metric, window)."""
        return dict(self._threshold_metadata)

    @property
    def threshold_caveat(self) -> str:
        return THRESHOLD_CAVEAT

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._pre.feature_names

    @property
    def window(self) -> int:
        return self._pre.window

    @property
    def n_features(self) -> int:
        return self._pre.n_features

    # ------------------------------------------------------------------ scoring

    def score(self, window: np.ndarray) -> DetectionResult:
        """Score one raw ``(30, 45)`` telemetry window."""
        results = self.score_batch(window[np.newaxis, ...] if np.asarray(window).ndim == 2
                                   else window)
        if len(results) != 1:
            raise ValueError(
                f"score() expects a single window; got a batch of {len(results)}. "
                f"Use score_batch()."
            )
        return results[0]

    def score_batch(self, windows: np.ndarray) -> list[DetectionResult]:
        """Score ``(n, 30, 45)`` raw telemetry windows."""
        # Imported here rather than at module scope purely for symmetry with the
        # tensor-level API; no circularity concern.
        from .scoring import score_windows

        prepared = self._pre.transform(windows)
        scores, errors = score_windows(self._model, prepared, device=self._device)
        return [
            DetectionResult(
                anomaly_score=float(scores[i]),
                threshold=self._threshold,
                is_anomaly=bool(scores[i] > self._threshold),
                feature_errors=errors[i],
                feature_names=self.feature_names,
            )
            for i in range(len(scores))
        ]

    def score_records(
        self, records: Sequence[Mapping[str, float]]
    ) -> DetectionResult:
        """Score 30 named telemetry readings (dicts keyed by feature name)."""
        prepared = self._pre.transform_records(records)
        from .scoring import score_windows

        scores, errors = score_windows(self._model, prepared, device=self._device)
        return DetectionResult(
            anomaly_score=float(scores[0]),
            threshold=self._threshold,
            is_anomaly=bool(scores[0] > self._threshold),
            feature_errors=errors[0],
            feature_names=self.feature_names,
        )
