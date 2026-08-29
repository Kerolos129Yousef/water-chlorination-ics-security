"""Regenerate the golden-vector fixtures from the verified SWaT dataset.

NOT part of the test suite -- ``test_golden_vectors.py`` reads the committed JSON
and needs no dataset. This script exists so the fixtures are reproducible rather
than magic numbers.

Run from the repository root with the dataset present::

    .venv/bin/python tests/fixtures/generate_golden_vectors.py

Source data is the reconstructed clean ``Attack_v0`` documented in
``docs/provenance/tranad_swat_provenance.md`` §6, loaded via
:func:`ml.src.dataset.load_attack_v0`::

    attack.csv (54,621 rows) + normal.csv rows 1..395,298
    -> merge, stable-sort by Timestamp  -> 449,919 rows, zero missing values
    -> subsample ::5 (SUBSAMPLE = 5)

Windows are identified by their subsampled start index so the selection is
explicit and re-derivable. Read-only: nothing under the dataset directory or
``ml/artifacts/`` is written.

Re-running this must produce a byte-identical ``golden_vectors.json``. It is a
generator, not a source of new truth -- if the output changes, something in the
loader or the inference chain changed and that is the finding, not the fixture.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ml.src import TranADDetector  # noqa: E402
from ml.src.dataset import attack_segments, default_dataset_dir, load_attack_v0  # noqa: E402
from ml.src.preprocessing import TranADPreprocessor  # noqa: E402

SUBSAMPLE = 5
WINDOW = 30
OUT = Path(__file__).resolve().parent / "golden_vectors.json"


def longest_attack_run(labels: np.ndarray) -> tuple[int, int]:
    """``(start, length)`` of the longest contiguous attack segment."""
    segments = attack_segments(labels)
    if not segments:
        raise SystemExit("no attack segments found; the dataset labels are wrong")
    start, end = max(segments, key=lambda se: se[1] - se[0])
    return start, end - start


def main() -> None:
    art = REPO / "ml" / "artifacts" / "swat_TranAD"
    feature_names = json.loads((art / "feature_names.json").read_text())
    dataset_dir = default_dataset_dir()

    full = load_attack_v0(feature_names, dataset_dir)
    print(f"Attack_v0 reconstruction: {len(full):,} rows, "
          f"{int(full.labels.sum()):,} attack ({full.attack_ratio:.4%})")

    sub = full.subsample(SUBSAMPLE)
    print(f"after ::{SUBSAMPLE}: {len(sub):,} rows")
    sub_v, sub_l, sub_t = sub.values, sub.labels, sub.timestamps

    # normal window: the very start of Attack_v0 (28/12 10:00:00 onward, pre-first-attack)
    normal_start = 0
    assert sub_l[normal_start : normal_start + WINDOW].max() == 0

    # attack window: fully inside the longest contiguous attack run
    best_start, best_len = longest_attack_run(sub_l)
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
                "first_timestamp": str(sub_t[start]).replace("T", " "),
                "last_timestamp": str(sub_t[start + WINDOW - 1]).replace("T", " "),
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
            "total_rows": int(len(full)),
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
    rendered = json.dumps(payload, indent=2) + "\n"
    if OUT.is_file() and OUT.read_text() == rendered:
        print(f"\n{OUT.relative_to(REPO)} unchanged (byte-identical)")
        return
    OUT.write_text(rendered)
    print(f"\nwrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()

