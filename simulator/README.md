# `simulator/` — SWaT Replay + Rolling Window (Phase 2A)

This layer turns the stored SWaT dataset into an **ordered telemetry stream** and
assembles that stream into the exact **`30 × 45` windows** the existing TranAD
detector consumes. It is the data-feeding stage of the MVP, built *before* the
API so that the rolling buffer has an explicit, tested owner before any HTTP
contract is designed.

```
SWaTReplay ──TelemetryRecord──▶ RollingWindow ──Window(30×45 raw)──▶ TranADDetector.score()
 owns data + pacing + segments   OWNS the 30-sample ring buffer      ml.src.detector (stateless)
```

There is **no** FastAPI, torch, or TranAD code in this package. The replay reuses
the provenance-backed reconstruction in `ml.src.dataset`; the buffer depends only
on numpy.

---

## Buffer ownership

The **`RollingWindow` owns the rolling 30-sample buffer** (a `deque(maxlen=30)`).
The **`TranADDetector` stays stateless** — it is handed a complete raw `(30, 45)`
window and returns a decision, holding nothing between calls. This is the
Phase 2A decision made concrete:

- A single reading is not scoreable; TranAD needs a filled 30-sample window
  (150 s of process time at the 5 s cadence — provenance §6).
- Keeping that state in one small, testable component (rather than in the
  detector or, later, in a request handler) means the detector remains trivially
  reusable and a future service can own or reset buffers per stream.

The buffer never mutates the records pushed into it: each sample's values are
validated and **copied** on ingest, and every emitted `Window` is a **fresh
array** that does not alias internal storage.

---

## Replay source

`SWaTReplay.from_artifacts()` / `from_dataset()` load the **established clean
`Attack_v0` reconstruction** via `ml.src.dataset.load_attack_v0`:

- `attack.csv` (54,621 rows) **+** `normal.csv` rows 1..395,298, merged and
  **stable-sorted by `Timestamp`** → **449,919 rows**, all 45 features populated,
  **12.1402 %** attack ratio, restoring the real normal → attack → normal sequence.
- `merged.csv` is **not** used (it carries the same 991,800-row gap). The raw CSVs
  are opened read-only and **never modified**.
- The reconstruction rule is **not duplicated here** — it lives once, in the
  provenance-backed loader (`docs/provenance/tranad_swat_provenance.md` §6).

Loading is a one-time batch step (the timestamp merge needs all rows); everything
after it is **streamed** — records and windows are produced lazily, one at a time,
so bounded memory regardless of dataset size.

### Cadence / subsampling

The shipped model + threshold were calibrated on `SUBSAMPLE = 5` data (1 Hz source
→ one sample per **5 s**). The replay subsamples by 5 by default, so a 30-sample
window spans **150 s**, matching training. Subsampling is in-memory only.

---

## Replay modes

| Mode | How |
|---|---|
| Full chronological | `replay.stream()` |
| Normal segment | `s, e = replay.normal_range(min_samples=30)` → `replay.stream(start=s, stop=e)` |
| Attack segment | `s, e = replay.attack_range(index=i, warmup=29)` → `replay.stream(start=s, stop=e)` |
| Longest attack (demo) | `i = replay.longest_attack_index()` → `replay.attack_range(i)` |

`attack_range` pads the segment with `warmup` preceding samples (default 29) so a
30-sample consumer's buffer is full exactly as the first attack sample arrives —
without introducing a time jump (padding stays within the chronological series).
`replay.attack_segments` exposes all 35 contiguous attack runs.

---

## Real-time factor

Between consecutive records the stream waits `sample_interval_seconds /
real_time_factor` (the first record is immediate):

| `real_time_factor` | Delay per step | Meaning |
|---|---|---|
| `1.0` | 5.0 s | wall-clock real time |
| `10` | 0.5 s | 10× faster |
| `100` | 0.05 s | 100× faster |
| `math.inf` | 0 s | no wait (tests / fast batch) |

Real timestamps inside each record are untouched — the factor controls *playback
speed*, not the data's time. `sleeper` is injectable so tests assert the intended
delays without ever sleeping 150 s.

---

## How the `30 × 45` window is produced

`RollingWindow.push(record)` appends one sample to the `deque(maxlen=30)` and:

- returns **`None`** until 30 samples are buffered (no output before the buffer
  fills),
- returns a **`Window`** on the **30th** push and on **every** push after (stride 1;
  the `maxlen` deque evicts the oldest sample automatically),
- **rejects** a record whose value vector is not length 45, or contains any
  non-finite value (a stuck/absent channel is a fault, not something to impute),
- preserves **feature order** and the 30 **timestamps** (oldest → newest).

Semantics match the offline `ml.src.dataset.sliding_windows` exactly, so the
online and batch paths agree.

---

## Integrating the existing detector

```python
import math
from ml.src.detector import TranADDetector
from simulator import SWaTReplay, RollingWindow

detector = TranADDetector.from_artifacts()
replay   = SWaTReplay.from_dataset(detector.feature_names)
buffer   = RollingWindow(detector.feature_names)

s, e = replay.attack_range(replay.longest_attack_index())
for window in buffer.stream(replay.stream(start=s, stop=e, real_time_factor=math.inf)):
    result = detector.score(window.values)     # window.values is raw (30, 45)
    if result.is_anomaly:
        print(window.last_timestamp, result.anomaly_score, result.top_features(3))
```

The detector is imported by the **consumer**, not by this package. Replay →
records, buffer → windows, detector → decision; each stage is independently
testable.

---

## End-to-end over the HTTP boundary (Phase 3)

`simulator/pipeline.py` wires the pieces together through the **FastAPI `/score`**
boundary — the integration that proves the whole path on real SWaT data:

```
SWaTReplay ─▶ RollingWindow ─▶ window_to_request ─▶ POST /score ─▶ TranAD ─▶ EndToEndResult
```

```python
import math
from simulator import SWaTReplay, run_pipeline
from simulator.pipeline import HttpScorer

replay = SWaTReplay.from_artifacts()
s, e = replay.normal_range(min_samples=30)
with HttpScorer("http://127.0.0.1:8000") as score:          # a running uvicorn
    for r in run_pipeline(replay, score, start=s, stop=e, real_time_factor=math.inf):
        print(r.window_end, r.is_anomaly, r.anomaly_score, r.top_features(3))
```

- **`window_to_request(window)`** — pure map from a `Window` to the `/score` body
  (feature_names + the raw `30 × 45` matrix + `window_start`/`window_end` from the
  window's own timestamps). No HTTP, no scoring.
- **`Scorer` / `HttpScorer`** — the transport is *injected*. `HttpScorer` POSTs to a
  live server (the production boundary; `httpx` is imported lazily). Tests inject a
  `TestClient`-backed callable that drives the identical ASGI app in-process.
- **`run_pipeline(replay, scorer, …)`** — streams replay → buffer → scorer, yielding
  one `EndToEndResult` per window. Defaults to `real_time_factor=inf` (no wait);
  buffering stays in `RollingWindow`, scoring stays behind the API.
- **`EndToEndResult`** — preserves window bounds, `anomaly_score`, `threshold`,
  `is_anomaly`, and the 45 per-feature attributions (plus ground-truth *metadata*
  for demo scoring — never a model input).

This module imports **no** `ml.src`, `torch`, or FastAPI — the boundary rules from
Phase 2A still hold; the detector is reached only through the injected scorer.

---

## Telemetry / sensor health (Phase 6A)

`simulator/telemetry_health.py` adds a **`TelemetryHealthMonitor`** at the same
ingest boundary as `RollingWindow` — a *separate observer* of the same telemetry
stream that detects **finite-but-stuck** continuous sensor channels:

```
TelemetryRecord ─▶ TelemetryHealthMonitor.observe() ─▶ TelemetryHealthResult
                └▶ RollingWindow.push() ─▶ Window ─▶ TranAD (ML path, unchanged)
```

```python
from simulator import TelemetryHealthMonitor
monitor = TelemetryHealthMonitor(detector.feature_names)   # defaults: continuous sensors, 2880 samples
for record in replay.stream():
    health = monitor.observe(record)
    if not health.healthy:
        print("TELEMETRY_FAULT", health.affected_features)   # distinct from an ML anomaly
```

- Flags a **`STUCK_CHANNEL`** when a *continuous* sensor's finite value is
  bit-identical for `max_unchanged_samples` consecutive samples.
- **Read-only**: never forward-fills, repairs, clips, or suppresses — the ML input
  is byte-identical whether or not a monitor is attached. Imports only numpy.
- Monitors **continuous sensors** (`FIT/LIT/AIT/DPIT/PIT`) by default; **discrete
  actuators** (`MV/P/UV`) legitimately hold state and are excluded (opt in via
  `monitored_features`).
- Default `max_unchanged_samples=2880` (4 h at the 5 s cadence) is **measured
  zero-false-positive** on the normal SWaT slice — see
  `docs/provenance/phase6a_telemetry_health.md` for the evidence and the
  false-positive-vs-threshold table.
- Telemetry faults stay **distinct** from ML `PROCESS_ANOMALY`s; both can be
  present on one window. Wire the monitor into `run_pipeline(..., health_monitor=…)`
  and the verdict rides on `EndToEndResult.telemetry_health`. By design it is
  **not** added to the thin, stateless FastAPI layer (per-sample stream state stays
  here at the ingest layer).

### Demo entrypoint

```bash
python scripts/run_e2e_demo.py --mode normal                 # clean segment → stays normal
python scripts/run_e2e_demo.py --mode attack                 # auto-picks the clearest attack
python scripts/run_e2e_demo.py --mode attack --base-url http://127.0.0.1:8000   # real socket
```

The attack demo **auto-selects** the attack segment that reads most clearly at
onset (the *longest* segment is not always the most detectable — some attacks ramp
in slowly); override with `--attack-segment N`. Defaults to the in-process ASGI
app so it runs with zero setup; `--base-url` proves a real HTTP socket. Requires
the licensed SWaT CSVs (read-only).

---

## Tests

- `tests/test_window_buffer.py` — buffer contract, synthetic and fast.
- `tests/test_replay.py` — replay behaviour (synthetic + `@pytest.mark.dataset`
  real-data), plus an end-to-end integration with the Phase 1 detector.
- `tests/test_integration_e2e.py` — Phase 3 full path (replay → window → HTTP
  `/score` → TranAD): real golden normal/attack detection, 30-sample warm-up,
  stride-1 across API calls, timestamp propagation, feature ordering, API-vs-direct
  parity, 422 on a bad window, no buffering state in FastAPI, artifact immutability,
  and accelerated pacing without real waiting. Uses golden vectors + tiny synthetic
  streams — no dataset required, runs in ~2 s.
- `tests/test_telemetry_health.py` — Phase 6A `TelemetryHealthMonitor` unit
  contract (28 cases): classification, stuck detection + boundary, non-finite
  handling, recovery, isolation, and configuration validation. Fully synthetic.
- `tests/test_telemetry_health_integration.py` — Phase 6A over the real ASGI app +
  TranAD (6 cases): fault surfaced through `run_pipeline`, ML path unaffected, ML
  anomaly + telemetry fault coexist and stay distinct, AlertEngine unaffected,
  `/status` unchanged.

Dataset-gated tests skip automatically when the licensed SWaT CSVs are absent.
