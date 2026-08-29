#!/usr/bin/env python3
"""Measure TranAD detection performance under four evaluation protocols.

Discharges provenance open item 3: *"Measure point-wise precision/recall/F1 at
the shipped threshold."*

Why four rungs instead of one number
------------------------------------
The research reported ``F1_fixed = 0.7934`` as the realistic deployment figure.
Three separate effects inflate it, and simply reporting a lower number would not
show which one mattered. Each rung changes exactly one thing:

    1. research concat  + any-in-window + point-adjusted   <- CONTROL: reproduce 0.7934
    2. clean Attack_v0  + any-in-window + point-adjusted   <- isolates the data defect
    3. clean Attack_v0  + last-timestep + point-adjusted   <- isolates the labelling bias
    4. clean Attack_v0  + last-timestep + point-wise       <- THE HONEST NUMBER

Rung 1 exists to make rungs 2-4 believable. If this harness cannot reproduce the
published figure on the published data, it has no standing to report a different
one -- and the script says so rather than burying it.

Rungs 2-4 share a single scoring pass; labelling and point-adjustment are
post-hoc on the same scores.

Nothing is retrained, no artifact is modified, and ``threshold.json`` is read
only. Usage::

    .venv/bin/python scripts/measure_detection_metrics.py
    .venv/bin/python scripts/measure_detection_metrics.py --skip-research
    SWAT_DATASET_DIR=/path/to/csvs .venv/bin/python scripts/measure_detection_metrics.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ml.src.dataset import (  # noqa: E402
    attack_segments,
    default_dataset_dir,
    load_attack_v0,
    load_research_concat,
)
from ml.src.detector import THRESHOLD_CAVEAT, TranADDetector  # noqa: E402
from ml.src.metrics import (  # noqa: E402
    MetricResult,
    score_metrics,
    threshold_free_metrics,
    window_labels_any,
    window_labels_last,
)
from ml.src.scoring import score_windows  # noqa: E402

ARTIFACTS = REPO / "ml" / "artifacts" / "swat_TranAD"
OUT_DOC = REPO / "docs" / "provenance" / "measured_detection_metrics.md"

SUBSAMPLE = 5
WINDOW = 30

# The figure this harness must reproduce on rung 1 to earn trust (notebook
# `F1_fixed`, provenance section 5.1).
RESEARCH_F1_FIXED = 0.7934
CONTROL_TOLERANCE = 0.05


@dataclass
class Rung:
    number: int
    dataset: str
    labelling: str
    protocol: str
    purpose: str
    result: MetricResult
    n_windows: int


def score_series(detector: TranADDetector, values: np.ndarray, label: str) -> np.ndarray:
    """Stream every stride-1 window through preprocessing and TranAD."""
    n_windows = len(values) - WINDOW + 1
    print(f"  scoring {n_windows:,} windows ({label}) ...", flush=True)
    t0 = time.perf_counter()
    scores: list[np.ndarray] = []
    done = 0
    for batch in detector._pre.transform_series(values):
        s, _ = score_windows(detector._model, batch)
        scores.append(s)
        done += len(batch)
        if done % 20_480 < len(batch):
            elapsed = time.perf_counter() - t0
            rate = done / elapsed
            print(
                f"    {done:>7,}/{n_windows:,}  {rate:,.0f} win/s  "
                f"eta {(n_windows - done) / rate:,.0f}s",
                flush=True,
            )
    out = np.concatenate(scores)
    print(f"  done in {time.perf_counter() - t0:,.0f}s", flush=True)
    assert len(out) == n_windows, (len(out), n_windows)
    return out


def fmt(r: MetricResult) -> str:
    return (
        f"P={r.precision:.4f} R={r.recall:.4f} F1={r.f1:.4f}  "
        f"[tp={r.true_positives:,} fp={r.false_positives:,} "
        f"fn={r.false_negatives:,} tn={r.true_negatives:,}]"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset-dir", type=Path, default=None,
        help="SWaT CSV directory (default: $SWAT_DATASET_DIR or ~/Downloads/SWaT/SWaT-dataset)",
    )
    ap.add_argument(
        "--skip-research", action="store_true",
        help="skip rung 1. Faster, but rungs 2-4 then have no harness validation.",
    )
    ap.add_argument("--no-write", action="store_true", help="print only, do not write the doc")
    args = ap.parse_args()

    dataset_dir = args.dataset_dir or default_dataset_dir()
    print(f"dataset : {dataset_dir}")
    print(f"artifacts: {ARTIFACTS}")

    detector = TranADDetector.from_artifacts(ARTIFACTS)
    threshold = detector.threshold
    feature_names = list(detector.feature_names)
    print(f"threshold: {threshold:.17g}\n")

    rungs: list[Rung] = []
    control_delta: float | None = None

    # ---------------------------------------------------------------- rung 1
    if not args.skip_research:
        print("[rung 1] research concat -- reproducing the published protocol")
        research = load_research_concat(feature_names, dataset_dir, subsample=SUBSAMPLE)
        print(
            f"  {len(research):,} samples  attack_ratio={research.attack_ratio:.4%}  "
            f"frozen_features={list(research.constant_features)}"
        )
        r_scores = score_series(detector, research.values, "research concat")
        r_labels = window_labels_any(research.labels, WINDOW)
        r_result = score_metrics(r_scores, r_labels, threshold, point_adjusted=True)
        control_delta = r_result.f1 - RESEARCH_F1_FIXED
        rungs.append(
            Rung(1, "research concat (defective)", "any-in-window", "point-adjusted",
                 "control: reproduce the published F1_fixed", r_result, len(r_scores))
        )
        print(f"  {fmt(r_result)}")
        print(f"  published F1_fixed={RESEARCH_F1_FIXED}  delta={control_delta:+.4f}")
        if abs(control_delta) <= CONTROL_TOLERANCE:
            print("  -> harness VALIDATED\n")
        else:
            print(
                f"  -> harness NOT validated (|delta| > {CONTROL_TOLERANCE}). "
                f"Treat the rungs below as unverified.\n"
            )
    else:
        print("[rung 1] SKIPPED -- rungs below are unvalidated against the research\n")

    # -------------------------------------------------------------- rungs 2-4
    print("[rungs 2-4] clean Attack_v0 -- one scoring pass, three protocols")
    clean = load_attack_v0(feature_names, dataset_dir, subsample=SUBSAMPLE)
    n_segments = len(attack_segments(clean.labels))
    print(
        f"  {len(clean):,} samples  attack_ratio={clean.attack_ratio:.4%}  "
        f"segments={n_segments}  constant_features={list(clean.constant_features)}"
    )
    c_scores = score_series(detector, clean.values, "clean Attack_v0")

    labels_any = window_labels_any(clean.labels, WINDOW)
    labels_last = window_labels_last(clean.labels, WINDOW)
    print(
        f"  window labels: any={labels_any.sum():,} positive, "
        f"last={labels_last.sum():,} positive "
        f"(+{labels_any.sum() - labels_last.sum():,} from any-in-window labelling)"
    )

    for number, labels, labelling, adjusted, purpose in (
        (2, labels_any, "any-in-window", True, "isolates the dataset defect"),
        (3, labels_last, "last-timestep", True, "isolates the labelling bias"),
        (4, labels_last, "last-timestep", False, "the honest deployment number"),
    ):
        res = score_metrics(c_scores, labels, threshold, point_adjusted=adjusted)
        rungs.append(
            Rung(number, "clean Attack_v0", labelling,
                 "point-adjusted" if adjusted else "point-wise",
                 purpose, res, len(c_scores))
        )
        print(f"  [rung {number}] {labelling:14s} {'point-adjusted' if adjusted else 'point-wise':14s} {fmt(res)}")

    free = threshold_free_metrics(c_scores, labels_last)
    print(
        f"\n  threshold-free (last-timestep): ROC-AUC={free['roc_auc']:.4f}  "
        f"PR-AUC={free['pr_auc']:.4f}  base_rate={free['base_rate']:.4%}"
    )

    honest = rungs[-1].result
    print(f"\n  HONEST point-wise F1 = {honest.f1:.4f}")
    print(f"  false-positive rate  = {honest.false_positive_rate:.4%} "
          f"(threshold was calibrated targeting 1%)")

    if not args.no_write:
        OUT_DOC.write_text(
            render_doc(rungs, free, control_delta, clean, n_segments, threshold, dataset_dir)
        )
        print(f"\nwrote {OUT_DOC.relative_to(REPO)}")
    return 0


def render_doc(rungs, free, control_delta, clean, n_segments, threshold, dataset_dir) -> str:
    honest = next(r for r in rungs if r.number == 4)
    by_num = {r.number: r for r in rungs}

    lines = [
        "# Measured detection performance — TranAD @ shipped threshold",
        "",
        "**Generated by** [`scripts/measure_detection_metrics.py`]"
        "(../../scripts/measure_detection_metrics.py) — re-runnable, not hand-transcribed.",
        f"**Threshold** `{threshold:.17g}` (unmodified, from `threshold.json`).",
        "**Model** unmodified. **Artifacts** read-only.",
        "",
        "This file discharges open item 3 of "
        "[`tranad_swat_provenance.md`](tranad_swat_provenance.md). It exists in version "
        "control because the research metrics tables never were — they lived only in "
        "meeting slides, which is how the `F1 = 0.9999` figure escaped its caveat.",
        "",
        "---",
        "",
        "## Headline",
        "",
        f"| | |",
        f"|---|---|",
        f"| **Point-wise F1 at the shipped threshold** | **{honest.result.f1:.4f}** |",
        f"| Precision | {honest.result.precision:.4f} |",
        f"| Recall | {honest.result.recall:.4f} |",
        f"| False-positive rate | {honest.result.false_positive_rate:.4%} |",
        f"| Research-reported `F1_fixed` (point-adjusted) | {RESEARCH_F1_FIXED} |",
        "",
        "Quote the first row as the deployment figure. Quote anything else only with its "
        "protocol stated.",
        "",
        "---",
        "",
        "## The ladder",
        "",
        "Each rung changes exactly one thing from the rung above, so the drop is "
        "attributable rather than merely smaller.",
        "",
        "| # | Dataset | Labelling | Protocol | Precision | Recall | F1 | Purpose |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    for r in sorted(rungs, key=lambda x: x.number):
        m = r.result
        lines.append(
            f"| {r.number} | {r.dataset} | {r.labelling} | {r.protocol} | "
            f"{m.precision:.4f} | {m.recall:.4f} | **{m.f1:.4f}** | {r.purpose} |"
        )
    lines += ["", "### Confusion counts", "",
              "| # | TP | FP | FN | TN | windows |", "|---|---:|---:|---:|---:|---:|"]
    for r in sorted(rungs, key=lambda x: x.number):
        m = r.result
        lines.append(
            f"| {r.number} | {m.true_positives:,} | {m.false_positives:,} | "
            f"{m.false_negatives:,} | {m.true_negatives:,} | {r.n_windows:,} |"
        )

    lines += ["", "---", "", "## Harness validation (rung 1)", ""]
    if control_delta is None:
        lines += [
            "**Not run** (`--skip-research`). The rungs above are therefore not validated "
            "against the published protocol. Re-run without the flag before publishing.",
        ]
    else:
        verdict = (
            "**validated**" if abs(control_delta) <= CONTROL_TOLERANCE
            else "**NOT validated**"
        )
        lines += [
            f"Reproducing the research protocol on the research data gives "
            f"`F1 = {by_num[1].result.f1:.4f}` against the published "
            f"`F1_fixed = {RESEARCH_F1_FIXED}` — delta `{control_delta:+.4f}`, {verdict} "
            f"(tolerance ±{CONTROL_TOLERANCE}).",
            "",
            "An exact match is not expected: the research applied its train/validation "
            "split under `SEED = 42` and forward-filled across a file boundary whose "
            "row ordering this harness reproduces but cannot bit-verify. What matters is "
            "that the harness lands on the published figure, so the lower numbers above "
            "are a protocol change and not a bug.",
        ]

    lines += [
        "",
        "---",
        "",
        "## Why each rung drops",
        "",
        "### Rung 1 → 2: the dataset defect",
        "",
        "The research scored `attack.csv + normal.csv` in full. 991,800 of those rows are "
        "a duplicated normal block in which six model features are frozen constants, and "
        "35.7% of normal rows are exact duplicates — which also deflates the attack ratio "
        "to 3.79% against the true 12.1402%. Rung 2 uses the reconstructed clean "
        f"`Attack_v0`: {len(clean):,} samples at {clean.attack_ratio:.4%} attack, "
        f"{n_segments} contiguous label segments, zero missing values.",
        "",
        "### Rung 2 → 3: the labelling bias",
        "",
        "TranAD reconstructs only the **last** timestep of each 30-sample window, so a "
        "score is a statement about timestep `i + 29`. The research labelled each window "
        "`1` if *any* of its 30 samples was an attack (notebook cell 11, `window_labels`).",
        "",
        "For an attack spanning samples `[a, b]` the two conventions agree exactly at "
        "onset, but any-in-window keeps asserting *attack* for 29 further windows — 145 s "
        "past the attack's end at the 5 s sample period. Those are the post-attack "
        "recovery windows, where reconstruction error is still elevated. Any-in-window "
        "scores them as true positives; last-timestep scores them as false positives.",
        "",
        "This is not in the original provenance record. It was found by reading the "
        "notebook source, since the notebook carries no saved outputs.",
        "",
        "### Rung 3 → 4: point adjustment",
        "",
        "`point_adjust` marks an entire attack segment detected if the model flagged any "
        "single point in it. It converts false negatives into true positives and never "
        "removes a false positive, so it can only raise the score. It is conventional in "
        "the SWaT literature, and Kim et al. (2022) show it systematically overstates "
        "performance — a random detector scores well under it.",
        "",
        "---",
        "",
        "## Threshold-free metrics (clean data, last-timestep labels)",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| ROC-AUC | {free['roc_auc']:.4f} |",
        f"| PR-AUC | {free['pr_auc']:.4f} |",
        f"| Base rate (attack prevalence) | {free['base_rate']:.4%} |",
        "",
        "PR-AUC is prevalence-sensitive. The research reported 0.8216 at a 3.79% base "
        "rate inflated by duplicate rows; the figure above is at the true rate, so the "
        "two are not comparable despite sharing a name.",
        "",
        "---",
        "",
        "## False-positive rate",
        "",
        f"Measured: **{honest.result.false_positive_rate:.4%}** "
        f"({honest.result.false_positives:,} false positives across "
        f"{honest.result.false_positives + honest.result.true_negatives:,} normal windows).",
        "",
        "`threshold.json` is the 99th percentile of normal-validation scores, so it "
        "targets 1% by construction. Provenance §5.3 predicted the real rate would exceed "
        "that because the calibration split had six frozen features. The measurement above "
        "is the test of that prediction.",
        "",
        "---",
        "",
        "## Reproducing",
        "",
        "```bash",
        f"SWAT_DATASET_DIR={dataset_dir} \\",
        "  .venv/bin/python scripts/measure_detection_metrics.py",
        "```",
        "",
        "Requires the licensed iTrust SWaT CSVs (not in version control). The full run "
        "scores every stride-1 window of both datasets.",
        "",
        "---",
        "",
        "## Caveat carried from the artifacts",
        "",
        "> " + THRESHOLD_CAVEAT.replace("\n", "\n> "),
        "",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
