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

## Tests

- `tests/test_window_buffer.py` — buffer contract, synthetic and fast.
- `tests/test_replay.py` — replay behaviour (synthetic + `@pytest.mark.dataset`
  real-data), plus an end-to-end integration with the Phase 1 detector.

Dataset-gated tests skip automatically when the licensed SWaT CSVs are absent.
