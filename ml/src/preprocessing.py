"""Production preprocessing chain for the TranAD SWaT model.

Reproduces the research pipeline exactly. The chain and every constraint it
enforces are documented in ``docs/provenance/tranad_swat_provenance.md`` §3 and
pinned in ``ml/configs/tranad_swat.yaml``::

    raw window (30, 45)  -- caller supplies, features in feature_names.json order
        -> StandardScaler.transform          per-feature, float64
        -> astype(float32)                   BEFORE MinMax (see _FLOAT32 note)
        -> reshape(n, 1350)                  C-order / timestep-major
        -> MinMaxScaler.transform            1350 columns, clip=False
        -> reshape(n, 30, 45)                float32, TranAD input

Three properties are load-bearing and each fails *silently* if broken, so each
has a dedicated regression test:

* **C-order flatten.** ``index = t * 45 + f``. The transposed layout scales all
  1350 columns by the wrong statistics and raises nothing.
* **clip=False.** Attack windows scale outside [0, 1]; that excursion is the
  detection signal. Clipping suppresses it.
* **1350 columns.** Mathematically the bounds are per-feature (all 30 window
  positions share them), but the artifact stores 1350 and we keep it that way
  for bit-exactness with the research run.

Deliberate divergence from the research code
--------------------------------------------
The notebook's ``load_dataset`` applied ``.ffill().fillna(0.0)`` across the whole
frame. That is exactly the mechanism that froze six model features and
contaminated threshold calibration (provenance §5.2). This module therefore
**rejects** non-finite input instead of imputing it: a live feed with a stuck or
absent channel is an operational fault the caller must handle, not something to
paper over. See ``docs/provenance/tranad_swat_provenance.md`` §5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

import joblib
import numpy as np

__all__ = ["TranADPreprocessor", "PreprocessingError"]

# The research pipeline cast windows to float32 in `make_windows` *before* the
# MinMax transform, so MinMax arithmetic ran in float32 against float32 scale_
# and min_ (confirmed: minmax_scaler.joblib stores float32). Casting later, or
# not at all, changes the low-order bits of every score.
_FLOAT32 = np.float32


class PreprocessingError(ValueError):
    """Raised when input does not satisfy the model's documented contract."""


@dataclass(frozen=True)
class _Artifacts:
    feature_names: tuple[str, ...]
    standard_scaler: object
    minmax_scaler: object


class TranADPreprocessor:
    """Turns raw SWaT telemetry windows into TranAD-ready tensors.

    Parameters
    ----------
    artifacts_dir:
        Directory holding ``feature_names.json``, ``scaler.joblib`` and
        ``minmax_scaler.joblib``. Read-only; never written to.
    window, n_features:
        Expected window length and feature count. Validated against the
        artifacts on construction, so a mismatched config fails loudly at
        startup rather than producing wrong scores at request time.
    """

    def __init__(
        self,
        artifacts_dir: str | Path,
        window: int = 30,
        n_features: int = 45,
    ) -> None:
        artifacts_dir = Path(artifacts_dir)
        self.window = int(window)
        self.n_features = int(n_features)

        names_path = artifacts_dir / "feature_names.json"
        ss_path = artifacts_dir / "scaler.joblib"
        mm_path = artifacts_dir / "minmax_scaler.joblib"
        for p in (names_path, ss_path, mm_path):
            if not p.is_file():
                raise FileNotFoundError(f"Required artifact missing: {p}")

        with names_path.open() as fh:
            feature_names = tuple(json.load(fh))
        standard_scaler = joblib.load(ss_path)
        minmax_scaler = joblib.load(mm_path)

        self._artifacts = _Artifacts(feature_names, standard_scaler, minmax_scaler)
        self._validate_artifacts()

    # ------------------------------------------------------------------ setup

    def _validate_artifacts(self) -> None:
        a = self._artifacts
        expected_flat = self.window * self.n_features

        if len(a.feature_names) != self.n_features:
            raise PreprocessingError(
                f"feature_names.json has {len(a.feature_names)} names but "
                f"n_features={self.n_features}"
            )
        if getattr(a.standard_scaler, "n_features_in_", None) != self.n_features:
            raise PreprocessingError(
                f"scaler.joblib expects "
                f"{getattr(a.standard_scaler, 'n_features_in_', None)} features, "
                f"config says {self.n_features}"
            )
        if getattr(a.minmax_scaler, "n_features_in_", None) != expected_flat:
            raise PreprocessingError(
                f"minmax_scaler.joblib expects "
                f"{getattr(a.minmax_scaler, 'n_features_in_', None)} columns, "
                f"config implies window*n_features={expected_flat}"
            )
        # clip=True would silently suppress the very excursions that signal an
        # attack, so refuse to run rather than degrade detection quietly.
        if getattr(a.minmax_scaler, "clip", False):
            raise PreprocessingError(
                "minmax_scaler.joblib has clip=True; the TranAD contract "
                "requires clip=False (see provenance doc §3)"
            )

    # --------------------------------------------------------------- properties

    @property
    def feature_names(self) -> tuple[str, ...]:
        """The 45 feature names, in the exact order the model expects."""
        return self._artifacts.feature_names

    @property
    def flat_dim(self) -> int:
        return self.window * self.n_features

    # ------------------------------------------------------------- validation

    def _as_batch(self, window: np.ndarray) -> tuple[np.ndarray, bool]:
        """Normalise (30, 45) or (n, 30, 45) to a 3-D batch. Returns (batch, was_single)."""
        arr = np.asarray(window)
        if arr.ndim == 2:
            arr = arr[np.newaxis, ...]
            single = True
        elif arr.ndim == 3:
            single = False
        else:
            raise PreprocessingError(
                f"expected a (window, n_features) or (batch, window, n_features) "
                f"array, got ndim={arr.ndim} shape={arr.shape}"
            )

        expected = (self.window, self.n_features)
        if arr.shape[1:] != expected:
            raise PreprocessingError(
                f"expected window shape {expected}, got {arr.shape[1:]}. "
                f"TranAD requires exactly {self.window} timesteps and "
                f"{self.n_features} features."
            )
        if arr.shape[0] == 0:
            raise PreprocessingError("empty batch")

        arr = arr.astype(np.float64, copy=False)
        if not np.isfinite(arr).all():
            bad = np.argwhere(~np.isfinite(arr))
            first = bad[0]
            name = self.feature_names[first[-1]]
            raise PreprocessingError(
                f"non-finite value at batch={first[0]} timestep={first[1]} "
                f"feature={name!r} ({len(bad)} total). This module does not "
                f"impute: a stuck or missing channel is an operational fault. "
                f"See provenance doc §5.2."
            )
        return arr, single

    def order_records(
        self, records: Sequence[Mapping[str, float]]
    ) -> np.ndarray:
        """Build an ordered ``(len(records), 45)`` array from named readings.

        Enforces an exact feature-name match. Missing *or* extra keys raise:
        silently dropping an extra channel, or defaulting a missing one, would
        misalign every downstream column against the scaler statistics.
        """
        if len(records) == 0:
            raise PreprocessingError("no records supplied")

        expected = set(self.feature_names)
        out = np.empty((len(records), self.n_features), dtype=np.float64)
        for i, rec in enumerate(records):
            got = set(rec)
            if got != expected:
                missing = sorted(expected - got)
                extra = sorted(got - expected)
                parts = []
                if missing:
                    parts.append(f"missing {missing}")
                if extra:
                    parts.append(f"unexpected {extra}")
                raise PreprocessingError(
                    f"record {i} feature mismatch: {'; '.join(parts)}. "
                    f"Expected exactly the {self.n_features} features in "
                    f"feature_names.json."
                )
            for j, name in enumerate(self.feature_names):
                out[i, j] = rec[name]
        return out

    # -------------------------------------------------------------- transform

    def transform(self, window: np.ndarray) -> np.ndarray:
        """Apply the full chain. Returns float32, shape matching the input rank.

        ``(30, 45) -> (1, 30, 45)`` is *not* squeezed back: downstream scoring is
        batch-oriented, so a single window is returned as a batch of one.
        """
        batch, _single = self._as_batch(window)
        n = batch.shape[0]

        # 1. StandardScaler over flattened rows (per-feature, pre-windowing).
        rows = batch.reshape(n * self.window, self.n_features)
        scaled = self._artifacts.standard_scaler.transform(rows)

        # 2. float32 cast, matching make_windows() in the research pipeline.
        scaled = scaled.reshape(n, self.window, self.n_features).astype(_FLOAT32)

        # 3. C-order flatten -> 1350, MinMax, reshape back.
        flat = scaled.reshape(n, self.flat_dim)          # C-order is numpy default
        flat = self._artifacts.minmax_scaler.transform(flat)
        out = flat.reshape(n, self.window, self.n_features)

        return np.ascontiguousarray(out, dtype=_FLOAT32)

    def transform_records(
        self, records: Sequence[Mapping[str, float]]
    ) -> np.ndarray:
        """``order_records`` then ``transform``, for named telemetry input."""
        ordered = self.order_records(records)
        if ordered.shape[0] != self.window:
            raise PreprocessingError(
                f"expected {self.window} records to fill one window, "
                f"got {ordered.shape[0]}"
            )
        return self.transform(ordered)

    def transform_series(
        self, rows: np.ndarray, chunk: int = 4096
    ) -> Iterator[np.ndarray]:
        """Stream overlapping windows from a continuous series of raw rows.

        Yields ``(k, 30, 45)`` float32 batches covering every stride-1 window of
        ``rows``, in order, where ``k <= chunk``. For ``n`` rows that is
        ``n - 29`` windows total.

        Equivalent to calling :meth:`transform` on each window separately, and
        **bit-identically so** -- not merely close. StandardScaler is affine and
        per-feature, so scaling before windowing produces the same float64 values
        as scaling after; the float32 cast and MinMax step then see identical
        inputs either way. A dedicated test asserts this against the golden
        windows, because a silent divergence here would corrupt every offline
        metric while leaving the online path correct.

        Two reasons this exists rather than looping :meth:`transform`:

        * **Work.** Stride-1 windows overlap 29/30, so per-window scaling
          standardises every row 30 times. Scaling once is what the research
          pipeline did (``make_splits`` scaled, then ``make_windows`` windowed).
        * **Memory.** Materialising all 89,955 clean-Attack_v0 windows as float32
          is 486 MB, and the flat MinMax stage needs as much again. Chunking
          bounds both to ~22 MB at the default.

        Input is validated *before* any batch is produced, so a bad call raises
        here rather than on first iteration.

        Parameters
        ----------
        rows:
            ``(n, 45)`` raw telemetry in ``feature_names`` order, ``n >= 30``.
            Validated for finiteness like :meth:`transform` -- a stuck or absent
            channel is an operational fault, not something to impute.
        """
        arr = np.asarray(rows)
        if arr.ndim != 2 or arr.shape[1] != self.n_features:
            raise PreprocessingError(
                f"expected a (n_samples, {self.n_features}) array, got shape "
                f"{arr.shape}"
            )
        if len(arr) < self.window:
            raise PreprocessingError(
                f"need at least {self.window} rows to form one window, "
                f"got {len(arr)}"
            )
        if chunk < 1:
            raise PreprocessingError(f"chunk must be >= 1, got {chunk}")

        arr = arr.astype(np.float64, copy=False)
        if not np.isfinite(arr).all():
            bad = np.argwhere(~np.isfinite(arr))
            first = bad[0]
            name = self.feature_names[first[-1]]
            raise PreprocessingError(
                f"non-finite value at row={first[0]} feature={name!r} "
                f"({len(bad)} total). This module does not impute: a stuck or "
                f"missing channel is an operational fault. See provenance doc §5.2."
            )

        return self._iter_series(arr, chunk)

    def _iter_series(self, arr: np.ndarray, chunk: int) -> Iterator[np.ndarray]:
        # Scale once, in float64, then window. The float32 cast stays *after*
        # windowing to match transform() exactly -- see the _FLOAT32 note.
        scaled = self._artifacts.standard_scaler.transform(arr)
        windows = np.lib.stride_tricks.sliding_window_view(
            scaled, window_shape=self.window, axis=0
        ).transpose(0, 2, 1)

        for start in range(0, len(windows), chunk):
            block = np.ascontiguousarray(
                windows[start : start + chunk], dtype=_FLOAT32
            )
            k = len(block)
            flat = self._artifacts.minmax_scaler.transform(
                block.reshape(k, self.flat_dim)
            )
            yield np.ascontiguousarray(
                flat.reshape(k, self.window, self.n_features), dtype=_FLOAT32
            )
