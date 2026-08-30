#!/usr/bin/env python
"""Phase 3 end-to-end demo: SWaT replay → rolling window → FastAPI → TranAD.

A CLI-level proof that the whole pipeline works on real SWaT data. It reuses the
production components unchanged -- it adds no detection logic and owns no buffer::

    SWaTReplay ─▶ RollingWindow ─▶ POST /score (FastAPI) ─▶ TranADDetector ─▶ result

Two modes, one pipeline (identical preprocessing and decision for both):

    python scripts/run_e2e_demo.py --mode normal      # a clean segment -> stays normal
    python scripts/run_e2e_demo.py --mode attack      # an attack segment -> anomaly flagged

Transport
---------
By default the demo drives the real ASGI app in-process (FastAPI TestClient):
same validation and serialisation as the network path, zero setup. To prove a
real HTTP socket instead, start the server and point the demo at it::

    uvicorn backend.app:app                      # terminal 1
    python scripts/run_e2e_demo.py --mode attack --base-url http://127.0.0.1:8000

Requires the licensed SWaT CSVs locally (set $SWAT_DATASET_DIR, or place them at
~/Downloads/SWaT/SWaT-dataset/). The raw CSVs are only read, never written.

This is intentionally the end of Phase 3: no alert engine, no dashboard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from simulator import SWaTReplay, run_pipeline  # noqa: E402
from simulator.pipeline import HttpScorer  # noqa: E402

WINDOW = 30
PROBE_WINDOWS = 3   # onset windows scored per segment when auto-selecting an attack


def _build_scorer(base_url: str | None):
    """Real HTTP to a running server (``--base-url``) or the in-process ASGI app."""
    if base_url:
        print(f"[transport] real HTTP → {base_url}")
        return HttpScorer(base_url), None
    from fastapi.testclient import TestClient

    from backend.app import create_app

    print("[transport] in-process ASGI (FastAPI TestClient)")
    client = TestClient(create_app())

    def _score(body):
        r = client.post("/score", json=dict(body))
        r.raise_for_status()
        return r.json()

    return _score, client


def _probe_onset_score(replay: SWaTReplay, scorer, seg_index: int) -> float:
    """Max anomaly score over a segment's first few (warmup-aligned) windows.

    A cheap proxy for "does this attack read as anomalous right away". Scored
    through the same HTTP scorer as the demo itself -- no detection logic here.
    """
    start, stop = replay.attack_range(seg_index)
    stop = min(stop, start + (WINDOW - 1) + PROBE_WINDOWS)
    return max((r.anomaly_score for r in run_pipeline(replay, scorer, start=start, stop=stop)),
               default=0.0)


def _select_attack_segment(replay: SWaTReplay, scorer) -> int:
    """Auto-pick the attack segment that detects most clearly at onset.

    The *longest* segment is not necessarily the most detectable (some attacks
    ramp in slowly), so instead of ``longest_attack_index()`` we probe each
    segment's first few windows and take the strongest. Keeps the attack demo
    an honest positive demonstration rather than a coin-flip on segment choice.
    """
    n = len(replay.attack_segments)
    return max(range(n), key=lambda i: _probe_onset_score(replay, scorer, i))


def _select_range(replay: SWaTReplay, mode: str, max_windows: int, scorer,
                  attack_segment: int | None) -> tuple[int, int]:
    """Pick a chronological [start, stop) range for the requested mode.

    Kept small by default so the demo is quick; a full-segment run is available
    via --max-windows 0 (unbounded). The attack range is warmup-padded so the
    first scored window ends exactly on the attack's first sample.
    """
    if mode == "normal":
        start, stop = replay.normal_range(min_samples=WINDOW)
    else:
        idx = attack_segment if attack_segment is not None else _select_attack_segment(replay, scorer)
        seg = replay.attack_segments[idx]
        print(f"[attack segment] index {idx}  samples {seg}  (length {seg[1] - seg[0]})")
        start, stop = replay.attack_range(idx)
    if max_windows > 0:
        stop = min(stop, start + (WINDOW - 1) + max_windows)
    return start, stop


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("normal", "attack"), default="attack")
    parser.add_argument("--base-url", default=None,
                        help="Score against a running server over real HTTP (default: in-process).")
    parser.add_argument("--max-windows", type=int, default=40,
                        help="Cap emitted windows for a quick demo (0 = whole segment).")
    parser.add_argument("--attack-segment", type=int, default=None,
                        help="Attack segment index (default: auto-select the clearest).")
    parser.add_argument("--real-time-factor", type=float, default=float("inf"),
                        help="Playback speed: inf = no wait (default), 60 = 60x live, 1 = real 5 s/step.")
    args = parser.parse_args()

    try:
        replay = SWaTReplay.from_artifacts()          # loads the clean Attack_v0 reconstruction
    except FileNotFoundError as exc:
        print(f"SWaT dataset not found: {exc}\n"
              f"Set $SWAT_DATASET_DIR or place the CSVs at ~/Downloads/SWaT/SWaT-dataset/.",
              file=sys.stderr)
        return 2

    scorer, _client = _build_scorer(args.base_url)
    start, stop = _select_range(replay, args.mode, args.max_windows, scorer, args.attack_segment)

    print(f"[mode] {args.mode}  [range] samples [{start}, {stop})  "
          f"({stop - start} samples → {max(0, (stop - start) - WINDOW + 1)} windows)")
    print("-" * 88)

    total = anomalies = attack_windows = detected_attacks = false_positives = 0
    for r in run_pipeline(replay, scorer, start=start, stop=stop,
                          real_time_factor=args.real_time_factor):
        total += 1
        anomalies += int(r.is_anomaly)
        gt_attack = r.contains_attack
        attack_windows += int(gt_attack)
        detected_attacks += int(gt_attack and r.is_anomaly)
        false_positives += int((not gt_attack) and r.is_anomaly)

        flag = "ANOMALY" if r.is_anomaly else "normal "
        gt = "attack" if gt_attack else "normal"
        top = ", ".join(f"{name}={err:.4g}" for name, err in r.top_features(3))
        print(f"{r.window_end}  [{flag}] score={r.anomaly_score:.6g} "
              f"thr={r.threshold:.6g}  gt={gt}  top: {top}")

    print("-" * 88)
    print(f"windows scored     : {total}")
    print(f"flagged anomalous  : {anomalies}")
    if args.mode == "attack":
        rate = (detected_attacks / attack_windows) if attack_windows else 0.0
        print(f"attack windows     : {attack_windows}")
        print(f"detected (recall)  : {detected_attacks} ({rate:.1%})")
    else:
        fpr = (false_positives / total) if total else 0.0
        print(f"false positives    : {false_positives} ({fpr:.1%})")
        print("note: threshold FPR caveat applies — see docs/provenance/tranad_swat_provenance.md §5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
