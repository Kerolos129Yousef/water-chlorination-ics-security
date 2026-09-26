# Phase 6A — Telemetry / Sensor-Health Hardening (stuck-channel detection)

**Status:** COMPLETE
**Scope:** detect finite-but-frozen sensor channels as a *telemetry-health* fault,
kept strictly separate from TranAD ML anomaly detection.
**ML artifacts:** untouched. No retraining, no threshold change, no artifact edit.
**Resolves:** `docs/provenance/tranad_swat_provenance.md` §7 open item 4.

---

## 1. Problem

The ingest path already rejects **non-finite** telemetry (NaN / ±inf) at both
`RollingWindow.push` and the API schema. But a sensor can be *technically finite
yet frozen*:

```
FIT101 = 0.0
FIT101 = 0.0
FIT101 = 0.0    ← finite, passes every existing check, but the channel is dead
...
```

A frozen channel can mean a failed sensor, a communication fault, or a stuck
PLC register. Provenance §7 item 4 flagged exactly this gap: *"a channel frozen
at a plausible constant still passes silently. Needs a stuck-channel check in the
replay/ingest layer."* This phase adds that check.

The hard part is **not** flagging legitimately-constant channels. Many SWaT
variables hold one value for long, entirely normal periods.

---

## 2. Design

### 2.1 Where it lives — ingest layer, next to `RollingWindow`

```
TelemetryRecord ──▶ TelemetryHealthMonitor.observe() ──▶ TelemetryHealthResult
                └──▶ RollingWindow.push() ──▶ Window ──▶ TranAD (ML path, unchanged)
```

`simulator/telemetry_health.py` — imports only numpy (no torch, FastAPI, `ml.src`).

Ownership boundary rationale:

* **Not in TranAD.** The detector is stateless and immutable; a stuck channel is
  an *operational* fault, not an ML anomaly. Conflating them is the mistake this
  phase avoids.
* **Not in FastAPI.** The backend is thin and stateless by rule — it sees isolated
  `30 × 45` windows, never the continuous per-sample stream a staleness counter
  requires. Per-sample streaming state belongs to the caller, where
  `RollingWindow` already lives. **`/status` was deliberately NOT extended** with
  telemetry-health for this reason.
* **Not in `RollingWindow`.** The buffer owns the raw ring and must not interpret
  physics. The monitor is a *separate observer* of the same stream.

### 2.2 It never touches the ML input

The monitor is read-only. It does **not** forward-fill, replace, clip, repair, or
suppress. It emits an explicit `TelemetryHealthResult`; the window handed to TranAD
is byte-identical whether or not a monitor is attached (proved by
`test_monitor_does_not_alter_ml_scoring`).

### 2.3 Signals stay distinguishable

An ML anomaly and a telemetry fault are independent fields on the pipeline result
(`is_anomaly` vs `telemetry_health`). A window can carry an ML anomaly, a telemetry
fault, both, or neither — verified end-to-end in
`test_telemetry_health_integration.py`. Terminology is kept distinct from the ML
side (`PROCESS_ANOMALY`): `STUCK_CHANNEL`, `TELEMETRY_FAULT`, `SENSOR_HEALTH`.

---

## 3. Heuristic & configuration

**Detect:** a *continuous* sensor whose finite value is **bit-identical for
`max_unchanged_samples` consecutive samples**.

* **Bit-identical equality**, not an epsilon band. A genuinely stuck digital/analog
  channel repeats the exact same number; live continuous telemetry carries
  measurement noise and effectively never does. Exact equality is deterministic
  and parameter-free.
* **Non-finite values never count toward a stuck run** — they are a different fault
  class, handled upstream, and can never raise a false `STUCK_CHANNEL`.

**Monitored set (default): continuous sensors only.** Classified by SWaT tag prefix:

| Class | Prefixes | Monitored? | Why |
|---|---|---|---|
| Continuous | `FIT`, `LIT`, `AIT`, `DPIT`, `PIT` | **yes** | flow/level/analyzer/pressure — should show live variation |
| Discrete actuator | `MV`, `P`, `UV` | **no** | valves/pumps/UV legitimately hold one state indefinitely (e.g. `P204` is constant across the entire dataset) — monitoring them is a guaranteed false positive |

The `P` (pump) prefix is matched as the full leading-letter run, so it never
collides with `PIT` (pressure). Unknown prefixes are excluded (fail-safe).
All parameters are configurable: `max_unchanged_samples`, `monitored_features`,
`sample_interval_seconds`. An operator may opt a discrete channel in explicitly.

**Default `max_unchanged_samples = 2880` (= 4 h at the 5 s effective cadence).**
Justified by measurement (§4).

---

## 4. Evaluation (normal SWaT)

Measured on the reconstructed clean **Attack_v0** at the shipped `subsample=5`
cadence (provenance §6): 89,984 samples, of which **79,053 are normal** across 36
contiguous normal segments (largest = 11,539 samples ≈ 16 h). 25 of 45 features
are continuous (monitored).

### 4.1 Longest legitimate constant run per class (normal data)

| Class | Worst-case normal run | Feature |
|---|---:|---|
| Continuous | **2,815 samples** (≈ 3.9 h) | `AIT401` (a near-quantised analyzer: only 15 distinct values) |
| Continuous (2nd) | 810 samples | `FIT601` (flow parks at 0 when its stage is idle) |
| Discrete | 11,539 samples (entire segment) | `P204` etc. — constant by design |

**No short global threshold is false-positive-free** — continuous sensors *do*
legitimately park at a baseline. This is the "legitimately stable process value vs
frozen telemetry" distinction, quantified.

### 4.2 False-positive rate vs threshold (continuous channels, normal data)

Flagged normal continuous sample-observations / total (1,976,325):

| `max_unchanged` | ≈ time | features triggered | FP rate |
|---:|---:|---:|---:|
| 30 | 2.5 min | 15 | 0.1069 |
| 60 | 5 min | 13 | 0.0799 |
| 120 | 10 min | 8 | 0.0525 |
| 180 | 15 min | 6 | 0.0382 |
| 240 | 20 min | 4 | 0.0287 |
| 300 | 25 min | 2 (`AIT401`, `FIT601`) | 0.0229 |
| 600 | 50 min | 2 | 0.0086 |
| 900 | 75 min | 1 (`AIT401`) | 0.0047 |
| 1500 | 125 min | 1 | 0.0021 |
| **2816** | **235 min** | **0** | **0.0000** |
| **2880 (default)** | **240 min** | **0** | **0.0000** |

### 4.3 Chosen default, verified

The longest continuous normal run is 2,815 samples, so any threshold `> 2815` is
zero-FP on this data. **2,880 (4 h)** is that safe value with margin, and it is a
natural operational figure.

Verified by streaming the real monitor over normal data:

* Largest contiguous normal segment (11,539 samples ≈ 16 h): **0 faults** at 2,880.
* All 36 normal segments (79,053 samples): **0 faults** at 2,880.

(`test_evaluation`-style figures reproduced by the analysis in the phase work; the
regression guard is the integration suite.)

---

## 5. Limitations (do not overstate)

* **Conservative by design.** The zero-FP default (4 h) detects only sustained /
  total freezes. A brief freeze below the threshold is not flagged. This favours
  precision (never cry wolf on normal telemetry) over fast detection. Operators
  who want faster detection can lower `max_unchanged_samples`, accepting the
  measured FP rate in §4.2 — dominated by `AIT401` and `FIT601`.
* **Zero-FP is claimed only for the measured normal SWaT slice**, not universally.
  A different plant, sensor set, or operating regime needs re-measurement.
* **Discrete actuators are not monitored by default.** A pump/valve position sensor
  that freezes is out of scope unless opted in — and opting in re-introduces the
  false-positive problem those channels have by nature.
* **Single-channel staleness only.** It does not detect correlated freezes, a
  channel stuck at a *drifting-but-wrong* value, or a channel that toggles between
  two frozen values. Those need cross-channel / physics-aware logic (future work).
* **Bit-identical equality** will miss a channel that dithers in its last bit while
  effectively dead. Not observed in SWaT; noted for completeness.

---

## 6. What it detects / does not detect (summary)

| | Detected? |
|---|---|
| Continuous sensor frozen at a finite constant for ≥ threshold | **yes** → `STUCK_CHANNEL` |
| NaN / ±inf telemetry | no (rejected upstream; separate fault class) |
| Discrete actuator held constant (normal) | no (by design — not a fault) |
| Continuous sensor legitimately parked < threshold | no (by design — not a fault) |
| ML process anomaly | no — that is TranAD's job, reported separately |

Interaction with ML: **orthogonal**. The telemetry-health verdict never changes,
gates, or suppresses the TranAD decision, and vice-versa. Both are surfaced; a
consumer decides what to do with each.

---

## 7. Test evidence

* `tests/test_telemetry_health.py` — 28 unit cases: classification, normal varying
  channel, legitimately-stable discrete channel, finite frozen channel, exact
  detection boundary, staleness reporting, multiple affected channels, recovery
  after resume, non-finite handling (no false stuck / breaks a run), monitor
  isolation, reset, and full configuration validation.
* `tests/test_telemetry_health_integration.py` — 6 integration cases over the real
  ASGI app + real TranAD: fault surfaced through `run_pipeline`, ML path
  unaffected by the monitor, ML anomaly + telemetry fault coexist and stay
  distinct, telemetry-only fault, AlertEngine unaffected (a telemetry fault never
  becomes an ML alert), and `/status` unchanged.

Full suite after this phase: **320 passing** (286 baseline + 34 new).

---

## 8. Future improvements

1. Optional per-feature thresholds (e.g. calibrate `AIT401` / `FIT601` separately)
   to allow faster detection on well-behaved channels without their FPs.
2. Cross-channel freeze correlation (a whole PLC block freezing at once).
3. Surface telemetry-health in an operator view via a *stateful* layer that legally
   owns stream state (e.g. the AlertEngine gaining a distinct `TELEMETRY_FAULT`
   category), not by adding state to FastAPI.
4. Detect "stuck at a plausible but wrong value" via slow-drift / physics checks.
