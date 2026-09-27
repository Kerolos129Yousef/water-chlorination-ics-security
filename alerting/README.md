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
- **Imports no** `torch`, FastAPI, `ml.src`, or `simulator`. Durability lives behind
  a small `AlertStore` (Phase 7); the engine still owns all lifecycle logic.

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
| `affected_channels` | Telemetry-fault only: the stuck channel(s); empty for a process anomaly |
| `reason` | Telemetry-fault only: cause string (`STUCK_CHANNEL: …`); null otherwise |
| `staleness_seconds` | Telemetry-fault only: how long the channel has been frozen |

## Telemetry faults — a second, independent track (Phase 6B)

The engine also turns telemetry-health verdicts (Phase 6A
`TelemetryHealthMonitor`) into **`TELEMETRY_FAULT`** incidents — kept **semantically
separate** from `PROCESS_ANOMALY`. A stuck sensor is *not* an ML anomaly.

```
TelemetryHealthResult ──▶ AlertEngine.process_health() ──▶ Alert(category=TELEMETRY_FAULT)
```

```
channel newly stuck     -> OPEN a fault for that channel
channel still stuck     -> EXTEND the same incident   (dedup: one incident per channel)
channel no longer stuck -> CLOSE it                    (deterministic recovery rule)
```

- **One incident per channel** — multiple stuck channels are multiple incidents, but
  never more than one open per channel. No correlation engine.
- **Recovery rule:** a channel's fault closes on the first verdict in which it is
  absent from `stuck_channels` (the monitor resets its counter the instant the value
  changes).
- **Two tracks, isolated:** `active_alert` is the ML slot (unchanged);
  `active_telemetry_faults` holds the open faults; `alerts` is the combined history.
  A telemetry fault never touches the ML slot and vice-versa — they may coexist.
- **Duck-typed, boundary intact:** `process_health` reads `TelemetryHealthLike` /
  `ChannelHealthLike`, so it imports nothing from `simulator`. A malformed or
  self-inconsistent verdict raises `ValueError`.
- Telemetry faults carry `is_anomaly=false`, inert ML fields, and a fixed
  placeholder severity — **category, not severity, carries the meaning.**

See `docs/provenance/phase6b_telemetry_fault_alerting.md`.

## Persistence (Phase 7)

Durable storage sits **behind** the engine, via a small `AlertStore` interface
(`alerting/store.py`). The engine makes every lifecycle decision and writes each
result through to the store; the store does no reasoning.

```
AlertEngine (lifecycle) ──write-through──▶ AlertStore ──▶ query() history / recovery
```

- **`InMemoryAlertStore`** (default) — non-durable, holds the live `Alert` objects;
  behaviour is identical to the pre-Phase-7 engine.
- **`SQLiteAlertStore`** — a single local file (auto-created), durable across
  restarts. One table, single-row `ON CONFLICT DO UPDATE` upserts (atomic, open
  order preserved), indexed on `status`/`category`.
- **`PostgreSQLAlertStore`** (Phase 8B) — the *same* schema and semantics on a
  PostgreSQL server, for a future multi-instance/cloud deployment. `psycopg` 3 with
  a connection pool (imported lazily, so memory/sqlite never require it); JSONB for
  the list columns, `seq BIGSERIAL` for open order, indexed on `seq`/`status`/
  `category`. See `docs/provenance/phase8b_postgres_alertstore.md`.

**Restart recovery:** a fresh engine on the same store re-adopts the open
`PROCESS_ANOMALY`, the per-channel `TELEMETRY_FAULT` incidents, and the history, and
resumes the id counters past every persisted id — so dedup/closure continue and no
duplicate incident or id is minted. `engine.alerts` reads the store;
`engine.active_alert` / `engine.active_telemetry_faults` are the live recovered
pointers.

**Config:** `ALERT_STORAGE_BACKEND=memory|sqlite|postgres` (via
`alert_store_from_env()`) — `ALERT_DB_PATH=…` for sqlite; `ALERT_PG_DSN` or the
`POSTGRES_*` parts for postgres (credentials from the environment, never source).
SQLite is the single-node MVP backend; PostgreSQL (Phase 8B) is the same interface
on a server, with **no** change to engine semantics. Runtime DB files are
git-ignored. See `docs/provenance/phase7_alert_persistence.md` and
`docs/provenance/phase8b_postgres_alertstore.md`.

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
engine.active_alert  # the currently-open PROCESS_ANOMALY, or None

# Telemetry faults (Phase 6B) — a separate track:
for verdict in health_stream:              # TelemetryHealthResult (or wire payload)
    engine.process_health(verdict)         # list of affected TELEMETRY_FAULT incidents
engine.active_telemetry_faults             # currently-open faults, one per stuck channel

# Durable persistence (Phase 7) — inject a store; a restart recovers state:
from alerting import AlertEngine, SQLiteAlertStore
engine = AlertEngine(store=SQLiteAlertStore("alerts.sqlite3"))
# ...restart... a fresh engine on the same file restores active incidents + history:
engine = AlertEngine(store=SQLiteAlertStore("alerts.sqlite3"))
```

## Tests

`tests/test_alert_engine.py` — no-alert/alert, schema fields (score, threshold,
timestamps), configurable top-N, deterministic ids, dedup of consecutive anomalies,
peak tracking, anomaly→normal close, independent alerts, attribution untouched, input
not mutated, heuristic severity, per-instance state isolation, `to_dict`, plus an
integration test running the real Phase 3 pipeline (replay → window → HTTP `/score` →
TranAD) into the engine. A dataset-gated test exercises a real attack segment's
open → dedup → close lifecycle (skipped when the SWaT CSVs are absent).

`tests/test_alert_engine_telemetry.py` (Phase 6B) — the telemetry-fault lifecycle:
open, dedup, active-while-stuck, recovery/close, re-open as a new incident, multiple
independent channels, ML-track independence, ML+telemetry coexistence, unchanged
PROCESS_ANOMALY lifecycle, `reset`, malformed-state rejection, plus a dataset-gated
SWaT check (a normal segment opens zero faults). `tests/test_telemetry_fault_api.py`
covers the same over the backend API (`/status` rollup, `/alerts` category + channels,
coexistence).

`tests/test_alert_store.py` (Phase 7) — the `AlertStore` contract on both backends
(SQLite file creation, upsert insert/update, filtered queries, round-trips, corrupt-row
safety, config). `tests/test_alert_persistence.py` — engine restart recovery for both
categories (open → extend → new engine on same DB → active restored → dedup continues →
close → history complete once), id no-collision, and the API serving recovered state.

## Not yet

Phase 7 added a durable **SQLite** backend and Phase 8B a **PostgreSQL** backend
behind the `AlertStore` interface (the default remains in-memory). Still not
implemented: **distributed multi-instance `AlertEngine` coordination** (PostgreSQL
makes storage server-grade, but two engine processes would each keep their own
in-memory active slot — out of scope for Phase 8B, see the phase8b doc),
notifications (email/SMS/Slack), acknowledgement, escalation, or paging. The
monitoring API and dashboard that render this engine's alerts were added in Phase
5A/6B; the engine itself still owns only lifecycle + deduplication.
