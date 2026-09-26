# Phase 6B — Telemetry Fault Surfacing / Operator Visibility

**Status:** COMPLETE
**Builds on:** Phase 6A (`docs/provenance/phase6a_telemetry_health.md`).
**Scope:** surface the *already-produced* `TelemetryHealthResult` to operators as a
first-class incident, kept semantically distinct from an ML anomaly.
**ML artifacts:** untouched. No retraining, no threshold change, no artifact edit.
**Detection threshold:** unchanged (Phase 6A default `max_unchanged_samples=2880`).

---

## 1. Objective

Phase 6A can *detect* a stuck/frozen continuous channel. It did not make that fault
visible to an operator. Phase 6B closes that gap **without** improving or changing
the detector: it takes the health verdict and turns a persistent fault into a
deduplicated, lifecycle-managed incident that the monitoring API and dashboard
expose alongside — never merged into — ML process anomalies.

An operator can now distinguish three states:

1. **PROCESS_ANOMALY** — TranAD reconstruction score over threshold.
2. **TELEMETRY_FAULT** — a monitored channel frozen past the staleness threshold.
3. **BOTH** — present on the same period, each reported in its own track.

`TELEMETRY_FAULT` is never collapsed into an ML anomaly, and vice-versa.

---

## 2. Architecture

The Phase 6A/2A invariants are all preserved:

```
raw telemetry
  ├─▶ RollingWindow            (owns the 30-sample buffer)              ─┐
  └─▶ TelemetryHealthMonitor   (owns per-sample staleness counters)     │  ingest layer
                                                                        │  (client side)
   window ──▶ POST /score ─────────────────────────────────────────────┘
   + optional telemetry_health verdict
        │
        ▼  (stateless FastAPI: validate → delegate → forward)
   TranADDetector.score()  ──▶ DetectionResult ──▶ AlertEngine.process()        [PROCESS_ANOMALY]
   request.telemetry_health ─────────────────────▶ AlertEngine.process_health() [TELEMETRY_FAULT]
        │
        ▼
   Monitoring API (/status, /alerts?category=…, /alerts/active) ──▶ Dashboard
```

Key decisions:

* **The stateful layer stays the `AlertEngine`.** It gains a *second, independent
  track* for telemetry faults (a per-channel dict), leaving the Phase 4 ML
  single-active-slot machine byte-for-byte unchanged. No new stateful component was
  introduced.
* **FastAPI stays stateless.** The stuck-channel monitor is a *per-sample stream*
  observer and remains at the ingest layer. The client computes the per-window
  verdict and attaches it to the existing `/score` request; the backend only
  *forwards* that single value to the engine (exactly as it already forwards the
  detection decision). No per-sample counters live in the API.
* **The ML input is never touched.** Forwarding a health verdict cannot alter,
  repair, or suppress the score. `/score`'s response contract is unchanged.
* **The engine imports nothing new from `simulator`.** `process_health` duck-types
  on `TelemetryHealthLike` / `ChannelHealthLike` protocols, so both the
  `TelemetryHealthResult` object and the backend's `TelemetryHealthPayload` wire
  model satisfy it. The boundary (`alerting` imports no `torch`/FastAPI/`ml.src`/
  `simulator`) holds.

---

## 3. Alert categories

`alerting/alert.py`:

| Constant | Value | Produced by |
|---|---|---|
| `PROCESS_ANOMALY_CATEGORY` | `"PROCESS_ANOMALY"` | TranAD via `AlertEngine.process()` |
| `TELEMETRY_FAULT_CATEGORY` | `"TELEMETRY_FAULT"` | monitor via `AlertEngine.process_health()` |

`DEFAULT_CATEGORY` remains an alias of `PROCESS_ANOMALY_CATEGORY` (Phase 4 back-compat).

The `Alert` dataclass gained three **defaulted** fields, populated only on the
telemetry track (empty/`None` for a process anomaly, so existing construction and
serialization are unchanged in meaning):

* `affected_channels: list[str]` — the stuck channel(s) (one per incident).
* `reason: str | None` — e.g. `STUCK_CHANNEL: FIT101 frozen (2880 unchanged samples)`.
* `staleness_seconds: float | None` — how long the channel has been frozen (latest).

A telemetry-fault alert carries `is_anomaly=False`, inert ML fields
(`anomaly_score=0.0`, `threshold=0.0`, `top_features=[]`), and a fixed placeholder
`severity=MEDIUM` — **category, not severity, carries the meaning** (there is no
calibrated telemetry-severity model, consistent with the project's existing
"severity is a heuristic" stance).

---

## 4. Lifecycle behaviour

`AlertEngine.process_health(result)` runs a per-channel state machine, entirely
separate from the ML slot:

```
channel newly stuck        -> OPEN a TELEMETRY_FAULT for that channel
channel still stuck        -> EXTEND the same incident (dedup: window_count++, span/staleness updated)
channel no longer stuck    -> CLOSE that incident            (deterministic recovery rule)
```

* **One incident per channel.** Multiple stuck channels → multiple incidents, but
  never more than one open per channel — dedup is inherent, no correlation engine.
* **Deterministic recovery rule.** A channel's incident closes on the *first* health
  verdict in which it is absent from `stuck_channels`. Because the Phase 6A monitor
  resets a channel's unchanged-counter the instant the value changes, "absent" means
  the channel resumed valid changing telemetry (or dropped below the threshold).
* **Deterministic ids.** `tfault-<seq>-<channel>-<timestamp>`, from a per-engine
  telemetry counter independent of the ML counter.
* **Consistency guard.** A verdict where `healthy` disagrees with `stuck_channels`
  (or a channel with no name / non-positive count) raises `ValueError` and is
  rejected — it never opens or drops an incident silently.

`process_health` returns the list of incidents affected (opened / extended / closed)
by that observation.

---

## 5. Coexistence model

The ML track and the telemetry track share nothing but the history list:

* `active_alert` — the single open **PROCESS_ANOMALY** (Phase 4, unchanged).
* `active_telemetry_faults` — the open **TELEMETRY_FAULT** incidents (new).
* `alerts` — full history of **both** categories, in open order.

Supported states, all covered by tests: **A** ML anomaly only, **B** telemetry fault
only, **C** both at once. A telemetry fault never opens/closes/touches the ML slot,
and a normal ML result never closes a telemetry fault.

---

## 6. API changes (backward compatible)

* `POST /score` — request gains an **optional** `telemetry_health` object
  (`TelemetryHealthPayload`: `healthy`, `fault_type`, `reason`, `timestamp`,
  `stuck_channels[]`). Omitting it behaves exactly as before. The **response
  contract is unchanged**. A malformed payload → `422` (score not affected); a
  well-shaped but self-inconsistent verdict is safely ignored (logged, best-effort),
  never corrupting state.
* `GET /status` — gains a bounded rollup: `active_telemetry_fault_count` and
  `telemetry_fault_channels`. No per-sample state is exposed.
* `GET /alerts` — gains an optional `category=PROCESS_ANOMALY|TELEMETRY_FAULT`
  filter. `AlertModel` gains `affected_channels`, `reason`, `staleness_seconds`
  (empty/`None` for process anomalies).
* `GET /alerts/active` — **unchanged**, still the ML active slot only (a telemetry
  fault never appears there).

The pipeline (`simulator/pipeline.py`) attaches the verdict automatically when a
`health_monitor` is supplied to `run_pipeline`, via the pure
`health_to_request_payload`.

---

## 7. Dashboard behaviour

`frontend/index.html` (same visual language, no redesign):

* System-status card shows a telemetry rollup: `N active` + the stuck channel
  name(s), or a green `healthy` badge.
* A new **Telemetry / sensor faults** card lists each active fault with its
  affected channel(s), how long it has been frozen, observation count, and reason.
* Two history tables: process-anomaly history (unchanged) and a new telemetry-fault
  history — the unified `/alerts` list is split client-side by `category`, so each
  track reads cleanly. Telemetry faults use a distinct amber badge/chip.

---

## 8. Tests

Baseline before Phase 6B: **320**. After: **351** (+31 new; 2 existing contract
tests updated — not weakened — for the intentionally extended `/status` rollup and
`AlertModel` serialization).

* `tests/test_alert_engine_telemetry.py` (21) — engine lifecycle: open, dedup,
  active-while-stuck, recovery/close, re-open as a new incident, multiple
  independent channels, ML-track independence, ML+telemetry coexistence, unchanged
  PROCESS_ANOMALY lifecycle, `reset` clears both tracks, and malformed-state
  rejection (inconsistent verdict, empty feature, bad count, missing field). Plus a
  dataset-gated SWaT check: the largest contiguous normal segment opens **zero**
  telemetry faults at the default 2880.
* `tests/test_telemetry_fault_api.py` (10) — over the real ASGI app: `/score`
  response unchanged with a health payload, malformed payload → 422, `/status`
  rollup opens/clears, `/alerts` category + affected-channel exposure, category
  filter separation, `/alerts/active` stays ML-only, ML+telemetry coexistence over
  the API, and the in-memory-only / resets-on-restart limitation.
* Updated: `tests/test_monitoring_api.py::test_alert_serialization_is_stable` (locks
  the extended AlertModel key set) and
  `tests/test_telemetry_health_integration.py::test_status_exposes_a_bounded_telemetry_rollup`.

---

## 9. Limitations

* **In-memory only.** All alert state (both tracks) is lost on backend restart — no
  persistence in this phase (deferred, as documented since Phase 4/5A).
* **Per-channel, single-sensor.** No correlated multi-sensor freeze detection and no
  drift detection (explicitly out of scope; noted in Phase 6A future work).
* **Telemetry severity is a fixed placeholder**, not a calibrated risk score.
* **The verdict is reported per scored window** (once the rolling buffer is warm).
  Faults that begin and end entirely inside the 30-sample warm-up, before any
  `/score` call, are observed by the monitor but not surfaced through the backend
  until the first window is scored. For the streaming MVP this is not a practical
  gap (the buffer warms in 150 s and the default fault threshold is 4 h).
* **Threshold unchanged.** Phase 6B deliberately did not revisit the 2880 default.

---

## 10. Future work

Persistence for alert history (the natural next step — Phase 7 candidate), optional
per-feature staleness thresholds, correlated-freeze detection, and notification/
escalation — all deferred.
