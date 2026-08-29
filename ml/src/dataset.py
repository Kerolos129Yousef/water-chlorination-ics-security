"""SWaT dataset loading for evaluation and replay.

Two entry points, for two different purposes:

* :func:`load_attack_v0` -- the **reconstructed clean Attack_v0**: gap-free,
  all 45 features populated, at the true 12.1402% attack ratio. This is the
  dataset for evaluation and for the MVP demo replay.
* :func:`load_research_concat` -- reproduces what the research notebook actually
  read, including the 991,800-row duplicated block with six frozen features. It
  exists **only** to validate an evaluation harness against the published
  numbers. Do not build anything on it.

The distinction is the subject of ``docs/provenance/tranad_swat_provenance.md``
sections 5 and 6, and it is the difference between a defensible metric and an
inflated one.

Dataset location
----------------
The CSVs are licensed from iTrust, SUTD and are **not** in version control
(``.gitignore`` excludes ``*.csv``). Point :envvar:`SWAT_DATASET_DIR` at them, or
pass ``dataset_dir`` explicitly. Everything here is read-only.

No pandas
---------
Deliberate: the inference library's only heavy dependencies are torch, numpy and
scikit-learn (``ml/requirements.txt``), and a CSV reader is not a good reason to
add another. The stdlib parse is also what keeps memory bounded -- the 449,919
rows land straight in preallocated numpy arrays instead of an intermediate list
of Python tuples.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

__all__ = [
    "SWaTSeries",
    "DatasetError",
    "load_attack_v0",
    "load_research_concat",
    "attack_segments",
    "sliding_windows",
    "default_dataset_dir",
    "ATTACK_V0_ROWS",
    "NORMAL_BLOCK1_ROWS",
]

# provenance section 5.2: normal.csv rows 1..395,298 are the Normal-labelled rows
# of the attack period and are gap-free. Everything after is the duplicated
# 22/12 -> 28/12 block whose seven leading-space columns are empty.
NORMAL_BLOCK1_ROWS = 395_298

# 54,621 (attack.csv) + 395,298 = the classic Attack_v0 row count.
ATTACK_V0_ROWS = 449_919

_TIMESTAMP_COL = "Timestamp"
_LABEL_COL = "Normal/Attack"

# SWaT timestamps look like "28/12/2015 10:00:00 AM". strptime is ~3x the cost of
# a manual parse over 450k rows but is the only form that stays obviously correct
# for the 12-hour clock, so it stays.
_TS_FORMAT = "%d/%m/%Y %I:%M:%S %p"


class DatasetError(ValueError):
    """Raised when the dataset on disk does not match its documented shape."""


def default_dataset_dir() -> Path:
    """``$SWAT_DATASET_DIR``, else the conventional local download location."""
    env = os.environ.get("SWAT_DATASET_DIR")
    if env:
        return Path(env)
    return Path.home() / "Downloads" / "SWaT" / "SWaT-dataset"


@dataclass(frozen=True)
class SWaTSeries:
    """A contiguous, time-ordered stretch of SWaT telemetry.

    Attributes
    ----------
    values:
        ``(n, 45)`` float64, columns in ``feature_names`` order -- i.e. already
        aligned to what :class:`~ml.src.preprocessing.TranADPreprocessor`
        expects, so no reordering happens downstream.
    labels:
        ``(n,)`` int8, 1 = attack. Ground truth per *sample*, not per window;
        converting to window labels is a decision with consequences, so it is
        left to :mod:`ml.src.metrics` rather than baked in here.
    timestamps:
        ``(n,)`` datetime64[s], strictly non-decreasing.
    constant_features:
        Features taking exactly one distinct value across this series. Measured,
        not assumed -- this is the check that catches a channel frozen at a
        physically plausible value, which the preprocessor's non-finite guard
        cannot see. See provenance section 5.2 and open item 4.
    """

    values: np.ndarray
    labels: np.ndarray
    timestamps: np.ndarray
    feature_names: tuple[str, ...]
    constant_features: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.values)

    @property
    def attack_ratio(self) -> float:
        return float(self.labels.mean())

    def subsample(self, step: int) -> "SWaTSeries":
        """Keep every ``step``-th sample, matching the research ``X.iloc[::5]``.

        ``constant_features`` is recomputed rather than carried over: a feature
        that varies in the full series can be constant in a subsample.
        """
        if step < 1:
            raise DatasetError(f"subsample step must be >= 1, got {step}")
        if step == 1:
            return self
        values = self.values[::step]
        return SWaTSeries(
            values=values,
            labels=self.labels[::step],
            timestamps=self.timestamps[::step],
            feature_names=self.feature_names,
            constant_features=_constant_features(values, self.feature_names),
        )


# --------------------------------------------------------------------- parsing

def _read_header(path: Path) -> list[str]:
    with path.open() as fh:
        line = fh.readline()
    if not line:
        raise DatasetError(f"{path} is empty")
    # Seven SWaT columns carry a leading space (" MV101", " AIT201", ...). The
    # research loader stripped them; matching that is what makes feature_names
    # line up with the scaler statistics.
    return [c.strip() for c in line.rstrip("\n").rstrip("\r").split(",")]


def _column_indices(
    header: Sequence[str], feature_names: Sequence[str], path: Path
) -> tuple[list[int], int, int]:
    missing = [n for n in feature_names if n not in header]
    if missing:
        raise DatasetError(
            f"{path.name} is missing {len(missing)} expected feature column(s): "
            f"{missing[:5]}{' ...' if len(missing) > 5 else ''}. "
            f"Header has {len(header)} columns."
        )
    for required in (_TIMESTAMP_COL, _LABEL_COL):
        if required not in header:
            raise DatasetError(f"{path.name} has no {required!r} column")
    return (
        [header.index(n) for n in feature_names],
        header.index(_TIMESTAMP_COL),
        header.index(_LABEL_COL),
    )


def _parse_label(raw: str) -> int:
    # Matches the notebook: lowercase, strip spaces, substring test. That also
    # absorbs the "A ttack" typo present in some SWaT releases.
    return 1 if "attack" in raw.strip().lower().replace(" ", "") else 0


def _iter_rows(
    path: Path,
    feature_names: Sequence[str],
    limit: int | None,
    *,
    allow_empty: bool,
) -> Iterator[tuple[datetime, int, list[float]]]:
    """Yield ``(timestamp, label, values)`` per data row.

    ``allow_empty`` distinguishes the two callers: the clean reconstruction
    rejects empty cells (they should not exist there, and silently imputing them
    is the exact mistake that contaminated threshold calibration), while the
    research reproduction must emit them so the caller can apply the notebook's
    forward-fill.
    """
    header = _read_header(path)
    cols, ts_i, lbl_i = _column_indices(header, feature_names, path)

    with path.open() as fh:
        fh.readline()  # header
        for n, line in enumerate(fh, start=1):
            if limit is not None and n > limit:
                break
            parts = line.rstrip("\n").rstrip("\r").split(",")
            if len(parts) != len(header):
                raise DatasetError(
                    f"{path.name} line {n + 1}: expected {len(header)} fields, "
                    f"got {len(parts)}"
                )
            values: list[float] = []
            for name, ci in zip(feature_names, cols):
                raw = parts[ci]
                if raw == "" or raw.isspace():
                    if not allow_empty:
                        raise DatasetError(
                            f"{path.name} line {n + 1}: empty value for {name!r}. "
                            f"The clean Attack_v0 reconstruction is gap-free by "
                            f"construction, so this means the row range is wrong "
                            f"(see provenance section 5.2)."
                        )
                    values.append(np.nan)
                else:
                    values.append(float(raw))
            yield datetime.strptime(parts[ts_i].strip(), _TS_FORMAT), _parse_label(
                parts[lbl_i]
            ), values


def _constant_features(
    values: np.ndarray, feature_names: Sequence[str]
) -> tuple[str, ...]:
    """Names of columns holding exactly one distinct value.

    Uses min == max rather than np.unique: same answer for this purpose, one
    pass, no sort, and no allocation proportional to the input.
    """
    if len(values) == 0:
        return ()
    frozen = values.min(axis=0) == values.max(axis=0)
    return tuple(name for name, f in zip(feature_names, frozen) if f)


def _collect(
    sources: Sequence[tuple[Path, int | None]],
    feature_names: Sequence[str],
    *,
    allow_empty: bool,
    sort_by_time: bool,
    expected_rows: int | None,
) -> SWaTSeries:
    n_features = len(feature_names)
    stamps: list[datetime] = []
    labels: list[int] = []
    chunks: list[np.ndarray] = []

    # Accumulate into fixed-size numpy blocks so peak memory stays ~n*45*8 bytes
    # rather than holding 450k Python lists alive at once.
    block_size = 65_536
    block = np.empty((block_size, n_features), dtype=np.float64)
    filled = 0

    for path, limit in sources:
        if not path.is_file():
            raise DatasetError(
                f"Dataset file not found: {path}. Set SWAT_DATASET_DIR or pass "
                f"dataset_dir explicitly."
            )
        for ts, label, values in _iter_rows(
            path, feature_names, limit, allow_empty=allow_empty
        ):
            if filled == block_size:
                chunks.append(block)
                block = np.empty((block_size, n_features), dtype=np.float64)
                filled = 0
            block[filled] = values
            filled += 1
            stamps.append(ts)
            labels.append(label)

    chunks.append(block[:filled])
    values_arr = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    if len(values_arr) == 0:
        raise DatasetError("no data rows read")
    if expected_rows is not None and len(values_arr) != expected_rows:
        raise DatasetError(
            f"expected {expected_rows:,} rows, got {len(values_arr):,}. The "
            f"dataset files do not match the shape recorded in the provenance "
            f"doc; verify the CSVs before trusting any metric derived from them."
        )

    stamps_arr = np.array(
        [np.datetime64(t, "s") for t in stamps], dtype="datetime64[s]"
    )
    labels_arr = np.array(labels, dtype=np.int8)

    if sort_by_time:
        # Stable sort restores the original normal -> attack -> normal sequence
        # after concatenating the label-filtered attack file with block 1 of the
        # normal file. Stability keeps same-second rows in file order.
        order = np.argsort(stamps_arr, kind="stable")
        values_arr = values_arr[order]
        labels_arr = labels_arr[order]
        stamps_arr = stamps_arr[order]

    names = tuple(feature_names)
    return SWaTSeries(
        values=values_arr,
        labels=labels_arr,
        timestamps=stamps_arr,
        feature_names=names,
        constant_features=_constant_features(values_arr, names),
    )


# ----------------------------------------------------------------- entry points

def load_attack_v0(
    feature_names: Sequence[str],
    dataset_dir: str | Path | None = None,
    *,
    subsample: int = 1,
    verify_rows: bool = True,
) -> SWaTSeries:
    """Load the reconstructed clean ``Attack_v0`` (provenance section 6).

    ``attack.csv`` (54,621 rows) + ``normal.csv`` rows 1..395,298, merged and
    stable-sorted by timestamp: 449,919 rows, zero missing values, all 45
    features populated, 12.1402% attack.

    Neither source alone is usable. ``merged.csv`` carries the identical
    991,800-row gap, and raw ``attack.csv`` is label-filtered -- its attack
    segments are concatenated with time discontinuities, so it is not a
    realistic telemetry stream.

    Parameters
    ----------
    subsample:
        Keep every n-th row. The research pipeline used 5 (1 Hz source -> one
        sample per 5 s), which is what the shipped model and threshold were
        calibrated under.
    verify_rows:
        Assert the 449,919-row count. Disable only for tests on synthetic data.
    """
    dataset_dir = Path(dataset_dir) if dataset_dir is not None else default_dataset_dir()
    series = _collect(
        [
            (dataset_dir / "attack.csv", None),
            (dataset_dir / "normal.csv", NORMAL_BLOCK1_ROWS),
        ],
        feature_names,
        allow_empty=False,
        sort_by_time=True,
        expected_rows=ATTACK_V0_ROWS if verify_rows else None,
    )
    return series.subsample(subsample) if subsample > 1 else series


def load_research_concat(
    feature_names: Sequence[str],
    dataset_dir: str | Path | None = None,
    *,
    subsample: int = 1,
) -> SWaTSeries:
    """Reproduce the data the research notebook actually trained and scored on.

    **This dataset is defective and is not for production use.** It exists so an
    evaluation harness can be validated against the published numbers before that
    harness is trusted to report better ones.

    Reproduces ``load_dataset`` from the notebook: ``FILE_GLOB = "[!mM]*.csv"``
    excludes ``merged.csv``, and ``sorted(glob(...))`` yields ``attack.csv`` then
    ``normal.csv`` -- alphabetical, *not* chronological, so the result is not in
    time order. 1,441,719 rows.

    Applies the notebook's ``.ffill().fillna(0.0)``, which is the mechanism that
    froze six model features across 100% of the validation split (provenance
    section 5.2). Reproducing the defect faithfully is the entire point.
    """
    dataset_dir = Path(dataset_dir) if dataset_dir is not None else default_dataset_dir()
    series = _collect(
        [
            (dataset_dir / "attack.csv", None),
            (dataset_dir / "normal.csv", None),
        ],
        feature_names,
        allow_empty=True,
        sort_by_time=False,  # notebook concatenates in glob order; no time sort
        expected_rows=None,
    )
    values = _forward_fill(series.values)
    filled = SWaTSeries(
        values=values,
        labels=series.labels,
        timestamps=series.timestamps,
        feature_names=series.feature_names,
        constant_features=_constant_features(values, series.feature_names),
    )
    return filled.subsample(subsample) if subsample > 1 else filled


def _forward_fill(values: np.ndarray) -> np.ndarray:
    """pandas ``.ffill().fillna(0.0)`` semantics, column-wise, in place.

    Carries the last valid observation forward; leading NaNs (no prior value)
    become 0.0.

    Done one column at a time deliberately. The whole-array form needs an int64
    index array the same shape as the input plus a gathered copy -- on the
    1,441,719-row research concat that is ~1.6 GB peak. Per column it is ~11 MB
    of scratch on top of the data itself.
    """
    out = values.copy()
    n_rows = len(out)
    if n_rows == 0:
        return out
    positions = np.arange(n_rows)
    for j in range(out.shape[1]):
        column = out[:, j]
        valid = ~np.isnan(column)
        if valid.all():
            continue
        idx = np.where(valid, positions, 0)
        np.maximum.accumulate(idx, out=idx)
        column = column[idx]
        # Positions with no preceding valid observation are still NaN -> 0.0.
        out[:, j] = np.nan_to_num(column, nan=0.0)
    return out


# -------------------------------------------------------------------- utilities

def attack_segments(labels: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous runs of ``label == 1`` as half-open ``[start, end)`` intervals.

    35 segments on the clean Attack_v0. The proposal's "36 attack scenarios"
    refers to attacks *launched* on the testbed; back-to-back attacks merge into
    one contiguous label run and some produced no labelled physical effect. Cite
    35 for this dataset's labels (provenance section 6).
    """
    labels = np.asarray(labels)
    if labels.ndim != 1:
        raise DatasetError(f"labels must be 1-D, got shape {labels.shape}")
    if len(labels) == 0:
        return []
    flagged = (labels == 1).astype(np.int8)
    edges = np.diff(np.concatenate(([0], flagged, [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def sliding_windows(values: np.ndarray, window: int = 30) -> np.ndarray:
    """``(n, f) -> (n - window + 1, window, f)`` stride-1, as a **view**.

    No copy: the result shares memory with ``values``, so materialising all
    89,955 clean-Attack_v0 windows costs nothing until something writes to them.
    Treat it as read-only.
    """
    values = np.asarray(values)
    if values.ndim != 2:
        raise DatasetError(
            f"expected a 2-D (n_samples, n_features) array, got shape {values.shape}"
        )
    if window < 1:
        raise DatasetError(f"window must be >= 1, got {window}")
    if len(values) < window:
        raise DatasetError(
            f"need at least {window} samples to form one window, got {len(values)}"
        )
    return np.lib.stride_tricks.sliding_window_view(
        values, window_shape=window, axis=0
    ).transpose(0, 2, 1)
