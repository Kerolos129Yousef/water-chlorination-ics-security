# `alerting/` — Alert Engine (Phase 4)

Turns a stream of per-window detector decisions into **deduplicated, lifecycle-managed
operator alerts**. This is the one intentionally **stateful** stage of the detection
path.

```
DetectionResult / EndToEndResult ──▶ AlertEngine.process() ──▶ Alert (OPEN → CLOSED)
```

## Responsibility (and boundaries)

- **Owns alert lifecycle + deduplication state** — the detector is stateless and the
  rolling window owns only raw samples; neither knows that many consecutive
  anomalous windows are *one* incident. That knowledge lives here, nowhere else.
- **Reuses the detector's output as-is.** It duck-types on the fields already
  produced (`is_anomaly`, `anomaly_score`, `threshold`, `top_features(n)`, and the
  optional `window_start` / `window_end`), so both `ml.src.detector.DetectionResult`
  and `simulator.pipeline.EndToEndResult` work unchanged. Feature attribution is
  copied **verbatim** from `top_features`; it is never recomputed.
- **Imports no** `torch`, FastAPI, `ml.src`, or `simulator`. **In-memory only** — no
  database in this phase.

## Lifecycle & deduplication

A tiny, explainable state machine over a single "currently-open" slot:

```
normal            -> (idle, no alert)
anomaly           -> OPEN a new alert
anomaly, anomaly  -> extend the SAME alert   (span + window_count grow; peak tracked)
normal            -> CLOSE the active alert
anomaly           -> OPEN a new, independent alert
```

So one continuous anomaly is **one** `Alert`, not one per overlapping 30-sample
window. While open, `anomaly_score` / `top_features` / `severity` track the **peak**
(most severe) window seen. Alert ids are deterministic (a per-engine counter), so the
same input stream always yields the same ids.

## Alert schema

`Alert.to_dict()` yields a JSON-friendly record:

| Field | Meaning |
|---|---|
| `alert_id` | Stable, deterministic identity |
| `category` | Finding type (default `PROCESS_ANOMALY`) |
| `status` | `OPEN` while the anomaly persists, `CLOSED` once normal returns |
| `detected_at` | Opening window's end timestamp |
| `window_start` / `window_end` | Span of the alert (first start … latest end) |
| `anomaly_score` / `threshold` | Peak score across the alert's windows, and the fixed threshold |
| `is_anomaly` | Always `true` for an emitted alert |
| `top_features` | Peak window's top-N `(feature, error)` pairs, from the detector |
| `window_count` | How many anomalous windows were merged |
| `severity` | Heuristic band — **not** an ML output (see below) |
| `opened_at` / `closed_at` | Lifecycle timestamps (`closed_at` null until closed) |

## Severity — engineering heuristic, not a prediction

There is **no research-validated severity classifier** in this project
(`ml/configs/tranad_swat.yaml: severity_bands_are_research_validated: false`,
`docs/provenance/tranad_swat_provenance.md` §5). `severity_for()` is a transparent,
deterministic banding of the `anomaly_score / threshold` ratio (`≥10×` → HIGH,
`≥2×` → MEDIUM, else LOW), provided only so an operator view can sort alerts. It must
always be presented as a heuristic.

## Usage

```python
from alerting import AlertEngine

engine = AlertEngine(top_n=5)
for result in detections:                 # DetectionResult or EndToEndResult
    alert = engine.process(result)        # None, or the affected Alert
engine.alerts        # every alert opened, in order (open + closed)
engine.active_alert  # the currently-open alert, or None
```

## Tests

`tests/test_alert_engine.py` — no-alert/alert, schema fields (score, threshold,
timestamps), configurable top-N, deterministic ids, dedup of consecutive anomalies,
peak tracking, anomaly→normal close, independent alerts, attribution untouched, input
not mutated, heuristic severity, per-instance state isolation, `to_dict`, plus an
integration test running the real Phase 3 pipeline (replay → window → HTTP `/score` →
TranAD) into the engine. A dataset-gated test exercises a real attack segment's
open → dedup → close lifecycle (skipped when the SWaT CSVs are absent).

## Not in this phase

No dashboard, database, notifications (email/SMS/Slack), acknowledgement, escalation,
or paging. This phase is only: detection decision → structured alert →
deduplication/lifecycle.
