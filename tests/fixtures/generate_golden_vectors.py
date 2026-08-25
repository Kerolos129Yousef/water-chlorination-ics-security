"""Regenerate the golden-vector fixtures from the verified SWaT dataset.

NOT part of the test suite -- ``test_golden_vectors.py`` reads the committed JSON
and needs no dataset. This script exists so the fixtures are reproducible rather
than magic numbers.

Run from the repository root with the dataset present::

    .venv/bin/python tests/fixtures/generate_golden_vectors.py

Source data is the reconstructed clean ``Attack_v0`` documented in
``docs/provenance/tranad_swat_provenance.md`` §6::

    attack.csv (54,621 rows) + normal.csv rows 1..395,298
    -> merge, stable-sort by Timestamp  -> 449,919 rows, zero missing values
    -> subsample ::5 (SUBSAMPLE = 5)

Windows are identified by their subsampled start index so the selection is
explicit and re-derivable. Read-only: nothing under the dataset directory or
``ml/artifacts/`` is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ml.src import TranADDetector  # noqa: E402
from ml.src.preprocessing import TranADPreprocessor  # noqa: E402

DATASET = Path(os.environ.get("SWAT_DATASET_DIR", Path.home() / "Downloads/SWaT/SWaT-dataset"))
NORMAL_BLOCK1_ROWS = 395_298          # provenance §5.2: rows 1..395,298 have no gaps
SUBSAMPLE = 5
WINDOW = 30
OUT = Path(__file__).resolve().parent / "golden_vectors.json"


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%d/%m/%Y %I:%M:%S %p")


def load_attack_v0(feature_names: list[str]) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """Return (values (N,45) float64, labels (N,) int, timestamps) for Attack_v0."""
    header = [c.strip() for c in (DATASET / "attack.csv").open().readline().rstrip("\n").split(",")]
    col_of = {name: header.index(name) for name in feature_names}
    ts_i, lbl_i = header.index("Timestamp"), header.index("Normal/Attack")

    rows: list[tuple[datetime, int, list[float]]] = []

    def ingest(path: Path, limit: int | None) -> None:
        with path.open() as fh:
            fh.readline()
            for n, line in enumerate(fh, start=1):
                if limit is not None and n > limit:
                    break
                p = line.rstrip("\n").split(",")
                lbl = 1 if "attack" in p[lbl_i].strip().lower().replace(" ", "") else 0
                vals = []
                for name in feature_names:
                    raw = p[col_of[name]]
                    if raw == "":
                        raise SystemExit(
                            f"unexpected empty value for {name} in {path.name} line {n+1}; "
                            "Attack_v0 is supposed to be gap-free (provenance section 5.2)"
                        )
                    vals.append(float(raw))
                rows.append((parse_ts(p[ts_i]), lbl, vals))

    ingest(DATASET / "attack.csv", None)
    ingest(DATASET / "normal.csv", NORMAL_BLOCK1_ROWS)

    rows.sort(key=lambda r: r[0])          # stable: restores original time order
    values = np.array([r[2] for r in rows], dtype=np.float64)
    labels = np.array([r[1] for r in rows], dtype=int)
    stamps = [r[0] for r in rows]
    return values, labels, stamps


def main() -> None:
    art = REPO / "ml" / "artifacts" / "swat_TranAD"
    feature_names = json.loads((art / "feature_names.json").read_text())

    values, labels, stamps = load_attack_v0(feature_names)
    print(f"Attack_v0 reconstruction: {len(values):,} rows, "
          f"{labels.sum():,} attack ({labels.mean():.4%})")
    assert len(values) == 449_919, len(values)

    sub_v, sub_l, sub_t = values[::SUBSAMPLE], labels[::SUBSAMPLE], stamps[::SUBSAMPLE]
    print(f"after ::{SUBSAMPLE}: {len(sub_v):,} rows")

    # normal window: the very start of Attack_v0 (28/12 10:00:00 onward, pre-first-attack)
    normal_start = 0
    assert sub_l[normal_start : normal_start + WINDOW].max() == 0

    # attack window: fully inside the longest contiguous attack run
    best_len = best_start = 0
    i = 0
    while i < len(sub_l):
        if sub_l[i] == 1:
            j = i
            while j < len(sub_l) and sub_l[j] == 1:
                j += 1
            if j - i > best_len:
                best_len, best_start = j - i, i
            i = j
        else:
            i += 1
    print(f"longest contiguous attack run in subsampled data: {best_len} samples at {best_start}")
    attack_start = best_start + (best_len - WINDOW) // 2
    assert sub_l[attack_start : attack_start + WINDOW].min() == 1

    detector = TranADDetector.from_artifacts(art)
    pre = TranADPreprocessor(art)

    cases = []
    for label, start in (("normal", normal_start), ("attack", attack_start)):
        raw = sub_v[start : start + WINDOW]
        prepared = pre.transform(raw)
        result = detector.score(raw)
        cases.append(
            {
                "name": label,
                "subsampled_start_index": int(start),
                "first_timestamp": sub_t[start].isoformat(sep=" "),
                "last_timestamp": sub_t[start + WINDOW - 1].isoformat(sep=" "),
                "window_label": int(sub_l[start : start + WINDOW].max()),
                "raw_window": [[float(x) for x in row] for row in raw],
                "expected": {
                    "preprocessed_sha256": hashlib.sha256(
                        np.ascontiguousarray(prepared, dtype=np.float32).tobytes()
                    ).hexdigest(),
                    "preprocessed_min": float(prepared.min()),
                    "preprocessed_max": float(prepared.max()),
                    "anomaly_score": float(result.anomaly_score),
                    "is_anomaly": bool(result.is_anomaly),
                    "feature_errors": [float(x) for x in result.feature_errors],
                },
            }
        )
        print(f"  {label:7s} start={start:<7} score={result.anomaly_score:.9e} "
              f"anomaly={result.is_anomaly} scaled_range=[{prepared.min():.4f}, {prepared.max():.4f}]")

    payload = {
        "_comment": (
            "Golden vectors for the TranAD SWaT inference pipeline. Regenerate with "
            "tests/fixtures/generate_golden_vectors.py. Tests read this file and do NOT "
            "need the dataset."
        ),
        "generated_by": "tests/fixtures/generate_golden_vectors.py",
        "environment": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "numpy": np.__version__,
        },
        "source": {
            "dataset": "reconstructed clean Attack_v0 (provenance section 6)",
            "files": ["attack.csv", "normal.csv rows 1..395298"],
            "total_rows": int(len(values)),
            "subsample": SUBSAMPLE,
            "window": WINDOW,
        },
        "contract": {
            "feature_names": feature_names,
            "threshold": detector.threshold,
            "n_features": detector.n_features,
        },
        "cases": cases,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nwrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
