"""SWaT telemetry replay: feed the clean Attack_v0 reconstruction as an ordered stream.

Phase 2A, the data-source half of the pipeline::

    SWaTReplay ──TelemetryRecord──▶ RollingWindow ──Window──▶ TranADDetector

Responsibilities (and, as importantly, non-responsibilities):

* Loads the **established** clean Attack_v0 reconstruction by delegating to
  :func:`ml.src.dataset.load_attack_v0`. It does **not** re-implement the
  reconstruction rule -- there is exactly one, it lives in the provenance-backed
  loader, and duplicating it here would be the classic way to let two "clean
  Attack_v0"s drift apart. See ``docs/provenance/tranad_swat_provenance.md`` §6.
* Preserves chronological ``Timestamp`` order (the loader stable-sorts).
* Exposes records in order, in normal / attack segments, at a configurable
  real-time factor.
* Contains **no TranAD logic** and **no windowing logic** -- it produces ordered
  telemetry records and stops there. Windowing is :mod:`simulator.window_buffer`;
  scoring is :mod:`ml.src.detector`.

Cadence. The shipped model and threshold were calibrated on ``SUBSAMPLE = 5`` data
(1 Hz source → one sample per 5 s), so the replay subsamples by 5 by default: a
30-sample window then spans 150 s, matching what TranAD was trained on. The raw
CSVs are never modified; subsampling happens in memory.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

import numpy as np

from ml.src.dataset import (
    SWaTSeries,
    attack_segments,
    default_dataset_dir,
    load_attack_v0,
)

__all__ = [
    "SWaTReplay",
    "TelemetryRecord",
    "ReplayError",
    "DATASET_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_SUBSAMPLE",
]

# provenance §6: SUBSAMPLE = 5 over a 1 Hz source → an effective 5 s sample period.
DATASET_SAMPLE_INTERVAL_SECONDS = 5.0
DEFAULT_SUBSAMPLE = 5

# Only used as a convenience default for warmup padding in attack_range(); the
# replay itself is window-size agnostic.
_TYPICAL_WINDOW = 30

DEFAULT_ARTIFACTS_DIR = (
    Path(__file__).resolve().parents[1] / "ml" / "artifacts" / "swat_TranAD"
)


class ReplayError(ValueError):
    """Raised on an invalid replay request (bad range, non-positive speed, ...)."""


@dataclass(frozen=True)
class TelemetryRecord:
    """One telemetry sample emitted by the replay.

    Attributes
    ----------
    timestamp:
        datetime64[s]. Real SWaT time, preserved so downstream can show *when*
        behaviour changed.
    values:
        ``(n_features,)`` float64 raw telemetry in ``feature_names`` order. A
        private copy -- mutating it cannot reach back into the loaded dataset.
    label:
        1 = attack, 0 = normal (per-sample ground truth).
    index:
        Position in the (subsampled) series, for traceability/debugging.
    """

    timestamp: np.datetime64
    values: np.ndarray
    label: int
    index: int


class SWaTReplay:
    """Replays a :class:`~ml.src.dataset.SWaTSeries` as ordered telemetry records.

    Construct from the real dataset::

        replay = SWaTReplay.from_artifacts()                 # uses feature_names.json
        for record in replay.stream(real_time_factor=100):   # 100x faster than live
            ...

    or from a pre-built series (used by fast unit tests, no CSVs needed)::

        replay = SWaTReplay(series)
    """

    def __init__(
        self,
        series: SWaTSeries,
        sample_interval_seconds: float = DATASET_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ReplayError("sample_interval_seconds must be > 0")
        self._series = series
        self.sample_interval_seconds = float(sample_interval_seconds)

    # ------------------------------------------------------------- construction

    @classmethod
    def from_dataset(
        cls,
        feature_names: Sequence[str],
        dataset_dir: str | Path | None = None,
        *,
        subsample: int = DEFAULT_SUBSAMPLE,
        sample_interval_seconds: float = DATASET_SAMPLE_INTERVAL_SECONDS,
        verify_rows: bool = True,
    ) -> "SWaTReplay":
        """Load the clean Attack_v0 reconstruction and wrap it.

        Delegates the reconstruction to :func:`ml.src.dataset.load_attack_v0`
        (attack.csv + normal.csv rows 1..395,298, merged and stable-sorted:
        449,919 rows, all 45 features populated, 12.1402% attack). Read-only.
        """
        series = load_attack_v0(
            feature_names, dataset_dir, subsample=subsample, verify_rows=verify_rows
        )
        return cls(series, sample_interval_seconds=sample_interval_seconds)

    @classmethod
    def from_artifacts(
        cls,
        artifacts_dir: str | Path = DEFAULT_ARTIFACTS_DIR,
        dataset_dir: str | Path | None = None,
        *,
        subsample: int = DEFAULT_SUBSAMPLE,
        sample_interval_seconds: float = DATASET_SAMPLE_INTERVAL_SECONDS,
        verify_rows: bool = True,
    ) -> "SWaTReplay":
        """Like :meth:`from_dataset`, but read the 45 feature names (and their
        order) straight from ``feature_names.json`` so the replay is guaranteed
        aligned with the model without the caller supplying them."""
        artifacts_dir = Path(artifacts_dir)
        feature_names = json.loads(
            (artifacts_dir / "feature_names.json").read_text()
        )
        if dataset_dir is None:
            dataset_dir = default_dataset_dir()
        return cls.from_dataset(
            feature_names,
            dataset_dir,
            subsample=subsample,
            sample_interval_seconds=sample_interval_seconds,
            verify_rows=verify_rows,
        )

    # --------------------------------------------------------------- properties

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._series.feature_names

    @property
    def n_features(self) -> int:
        return len(self._series.feature_names)

    def __len__(self) -> int:
        return len(self._series)

    @property
    def attack_ratio(self) -> float:
        return self._series.attack_ratio

    @property
    def attack_segments(self) -> list[tuple[int, int]]:
        """Contiguous attack runs as half-open ``[start, stop)`` indices.

        Reuses :func:`ml.src.dataset.attack_segments` -- 35 segments on the full
        clean Attack_v0 (provenance §6)."""
        return attack_segments(self._series.labels)

    # ------------------------------------------------------------ record access

    def record_at(self, i: int) -> TelemetryRecord:
        """Build the record for series index ``i`` (values copied out)."""
        if not 0 <= i < len(self._series):
            raise ReplayError(f"index {i} out of range [0, {len(self._series)})")
        return TelemetryRecord(
            timestamp=self._series.timestamps[i],
            values=np.array(self._series.values[i], dtype=np.float64, copy=True),
            label=int(self._series.labels[i]),
            index=i,
        )

    # -------------------------------------------------------------- segment API

    def normal_range(self, min_samples: int = _TYPICAL_WINDOW) -> tuple[int, int]:
        """First contiguous all-normal run of at least ``min_samples`` samples.

        ``min_samples`` defaults to a 30-sample window's worth so the range can
        produce at least one window; pass your consumer's window size to be sure.
        """
        labels = self._series.labels
        n = len(labels)
        i = 0
        while i < n:
            if labels[i] == 0:
                j = i
                while j < n and labels[j] == 0:
                    j += 1
                if j - i >= min_samples:
                    return (i, j)
                i = j
            else:
                i += 1
        raise ReplayError(f"no contiguous normal run of >= {min_samples} samples")

    def attack_range(
        self, index: int = 0, *, warmup: int = _TYPICAL_WINDOW - 1
    ) -> tuple[int, int]:
        """Half-open range covering attack segment ``index``, padded with
        ``warmup`` preceding samples.

        ``warmup`` defaults to 29 so that a 30-sample consumer has a full buffer
        exactly as the first attack sample arrives -- i.e. the first emitted
        window's final (scored) timestep is the attack's first sample. Padding
        stays within the chronological series, so no artificial time jump is
        introduced.
        """
        segments = self.attack_segments
        if not segments:
            raise ReplayError("dataset has no attack segments")
        if not 0 <= index < len(segments):
            raise ReplayError(
                f"attack segment index {index} out of range [0, {len(segments)})"
            )
        if warmup < 0:
            raise ReplayError(f"warmup must be >= 0, got {warmup}")
        start, stop = segments[index]
        return (max(0, start - warmup), stop)

    def longest_attack_index(self) -> int:
        """Index (into :attr:`attack_segments`) of the longest attack run.

        Convenient for a demo/integration: the longest segment is the most
        reliably detected.
        """
        segments = self.attack_segments
        if not segments:
            raise ReplayError("dataset has no attack segments")
        return max(range(len(segments)), key=lambda k: segments[k][1] - segments[k][0])

    # ---------------------------------------------------------------- streaming

    def stream(
        self,
        *,
        start: int = 0,
        stop: int | None = None,
        real_time_factor: float = 1.0,
        sleeper: Callable[[float], None] | None = time.sleep,
    ) -> Iterator[TelemetryRecord]:
        """Yield records for ``[start, stop)`` in order, paced by ``real_time_factor``.

        Pacing
        ------
        Between consecutive records the stream waits
        ``sample_interval_seconds / real_time_factor`` (the first record is
        immediate). So ``real_time_factor=1`` is wall-clock real time (5 s/step),
        ``10`` is 10x faster, ``100`` is 100x faster. ``math.inf`` disables the
        wait entirely -- the right choice for tests and for as-fast-as-possible
        batch consumption, so nothing ever sleeps 150 s to fill a window.

        ``sleeper`` is injectable purely so tests can assert the *intended* delays
        without real waiting; leave it at :func:`time.sleep` in production, or set
        it to ``None`` to skip waiting regardless of factor.

        The real timestamps travel inside each record untouched -- pacing controls
        playback speed, it does not rewrite time.
        """
        n = len(self._series)
        stop = n if stop is None else stop
        if not (0 <= start < stop <= n):
            raise ReplayError(
                f"invalid range [{start}, {stop}) for a series of length {n}"
            )
        if real_time_factor <= 0 or math.isnan(real_time_factor):
            raise ReplayError(
                f"real_time_factor must be > 0, got {real_time_factor}"
            )

        delay = (
            0.0 if math.isinf(real_time_factor)
            else self.sample_interval_seconds / real_time_factor
        )
        values = self._series.values
        labels = self._series.labels
        timestamps = self._series.timestamps

        for i in range(start, stop):
            if i > start and delay > 0.0 and sleeper is not None:
                sleeper(delay)
            yield TelemetryRecord(
                timestamp=timestamps[i],
                values=np.array(values[i], dtype=np.float64, copy=True),
                label=int(labels[i]),
                index=i,
            )
