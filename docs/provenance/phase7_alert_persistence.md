# Phase 7 — Alert Persistence

**Status:** COMPLETE
**Builds on:** Phase 6B (`docs/provenance/phase6b_telemetry_fault_alerting.md`).
**Scope:** make alert/incident state durable across process restarts, preserving
every existing lifecycle semantic and the two-category model.
**ML artifacts / threshold / telemetry-health logic:** untouched.

---

## 1. Objective

Since Phase 4 all alert state has been in-memory and lost on restart. Phase 7 adds
durable storage **behind** the `AlertEngine` so that after a restart:

- history survives,
- active `PROCESS_ANOMALY` and `TELEMETRY_FAULT` incidents are recovered,
- deduplication and closure continue seamlessly,
- deterministic ids never collide.

This is a **persistence** change only — no detection, threshold, or telemetry-health
behaviour is altered.

---

## 2. Architecture

```
signals ─▶ AlertEngine (all lifecycle: open/extend/close/dedup/severity/category)
                 │  write-through
                 ▼
           AlertStore  ──────────────▶  Monitoring API ─▶ Dashboard
        (durable; no lifecycle logic)     (unchanged surface)
```

* **The `AlertEngine` keeps owning every lifecycle decision.** It holds the live
  working pointers it needs for O(1) decisions — the single ML active slot
  (`_active`) and the per-channel telemetry map (`_telemetry_active`) — and writes
  each result through to the store. The store is the **source of truth for
  history**; `engine.alerts` reads it.
* **The store does no reasoning.** It stores and returns whole `Alert` records.
* **FastAPI stays stateless** and gains no per-sample state: it still only *drives*
  the engine on `/score` and *reads* it on the monitoring endpoints. Persistence
  lives entirely behind the engine.
* **Swappable backend.** One small `AlertStore` interface, two implementations —
  the SQLite store can later be replaced by PostgreSQL/cloud with **no change to
  `AlertEngine` semantics**.

---

## 3. Storage technology & schema

**SQLite** — a single local file, no server to install, created automatically. The
smallest technology that gives durable restart behaviour for a local MVP, and a
natural precursor to PostgreSQL. Standard-library `sqlite3` only (no ORM: the
access pattern is single-table single-row upserts + simple filtered reads, so an
ORM would add dependency and indirection without materially improving
maintainability).

Interface (`alerting/store.py`):

```python
class AlertStore(Protocol):
    def upsert(self, alert: Alert) -> None
    def query(self, *, category=None, status=None, limit=None) -> list[Alert]   # open order
    def clear(self) -> None
    def close(self) -> None
```

Backends: `InMemoryAlertStore` (default; non-durable, holds live objects — exact
pre-Phase-7 behaviour) and `SQLiteAlertStore`.

Schema — one table, one row per incident (both categories; category-specific
columns are nullable):

| column | type | notes |
|---|---|---|
| `alert_id` | TEXT PK | deterministic id; conflict target for upsert |
| `category` | TEXT | `PROCESS_ANOMALY` / `TELEMETRY_FAULT` |
| `status` | TEXT | `OPEN` / `CLOSED` |
| `detected_at`, `window_start`, `window_end`, `opened_at`, `closed_at` | TEXT | lifecycle timestamps (opaque strings) |
| `anomaly_score`, `threshold` | REAL | ML fields (0.0 for telemetry) |
| `is_anomaly` | INTEGER | 1/0 |
| `top_features` | TEXT | JSON `[[name, error], …]` (empty for telemetry) |
| `window_count` | INTEGER | dedup counter |
| `severity` | TEXT | heuristic band (placeholder for telemetry) |
| `affected_channels` | TEXT | JSON list (telemetry: `["FIT101"]`; ML: `[]`) |
| `reason` | TEXT NULL | telemetry cause string |
| `staleness_seconds` | REAL NULL | telemetry freeze duration |

Indexes on `status` and `category`. History order is `ORDER BY rowid`; writes use
`INSERT … ON CONFLICT(alert_id) DO UPDATE` (not `INSERT OR REPLACE`) so a row's
`rowid` — hence open order — is preserved across updates.

---

## 4. Configuration

Environment-driven, defaulting to the pre-Phase-7 behaviour:

| variable | values | default |
|---|---|---|
| `ALERT_STORAGE_BACKEND` | `memory` \| `sqlite` | `memory` |
| `ALERT_DB_PATH` | file path | `alerts.sqlite3` (CWD) — used only for `sqlite` |

`alert_store_from_env()` builds the store; `create_app()` uses it when no engine is
injected. Defaulting to `memory` keeps unit tests and any un-configured caller on
the exact previous behaviour; durability is opt-in. A fresh checkout creates the
SQLite file (and parent dirs) automatically on first use. Runtime DB files
(`*.sqlite3`, `-wal`, `-shm`, `*.sqlite`, `*.db`) are git-ignored.

---

## 5. Restart / recovery semantics

On construction the engine calls `_recover()`, which reads the store once and:

1. re-adopts the open `PROCESS_ANOMALY` into `_active`,
2. re-adopts every open `TELEMETRY_FAULT` into `_telemetry_active` (keyed by
   channel),
3. resumes the id counters (`_seq`, `_tseq`) **past every persisted id**.

Because the counters resume, a restart mints no colliding ids; because active
incidents are re-adopted, deduplication and closure continue on the *same*
incident rather than opening a duplicate; because history lives in the store, it is
never erased. A restart does **not** close active incidents (status is loaded
as-is). ML scores and telemetry-health detection are untouched.

Counter recovery is by parsing the sequence embedded in the id (`alert-0007-…` → 7,
`tfault-0003-FIT101-…` → 3); malformed ids contribute 0 and cannot lower the max.

---

## 6. Failure handling

* **Single-row, atomic writes.** Each lifecycle step touches exactly one alert row
  via one autocommitted upsert — there is no multi-row transaction to leave
  half-written.
* **Open is persist-then-adopt.** A new incident is written to the store *before*
  the in-memory pointer is set, so a write failure raises without leaving a
  dangling in-memory incident. Extend/close mutate-then-persist (the row already
  exists).
* **No silent success.** `SQLiteAlertStore` raises `AlertStoreError` (logged with
  context) on any write failure — it never pretends a write succeeded. In the
  backend, `/score`'s monitoring update is best-effort (the failure is logged and
  swallowed so scoring is unaffected), consistent with Phase 5A.
* **Corrupt data is non-fatal.** A single unreadable row (e.g. bad JSON) is skipped
  with a warning during load; valid rows still recover.

No distributed transactions, WAL tuning, or connection pooling — out of scope for a
local MVP.

---

## 7. API & dashboard impact

**None to the contract.** `POST /score`, `GET /status`, `GET /alerts`,
`GET /alerts/active` are byte-compatible. The distinction between active ML
anomalies, active telemetry faults, and combined history is preserved. The only
observable change is the intended one: with `sqlite`, alerts remain visible after a
backend restart. The dashboard is unchanged and simply keeps rendering the
(now-durable) API state.

---

## 8. Tests

Baseline before Phase 7: **351**. After: **381** (+30 new; no existing test
modified, deleted, or weakened).

* `tests/test_alert_store.py` (19) — both backends: SQLite file/parent-dir creation,
  insert-then-update upsert (no duplicate, open order preserved across updates),
  category/status/limit queries, PROCESS_ANOMALY and TELEMETRY_FAULT field
  round-trips, `clear`, in-memory isolation, corrupt-row skip, and
  `alert_store_from_env` config (memory default, sqlite selection, unknown backend
  rejected, case-insensitivity).
* `tests/test_alert_persistence.py` (11) — engine restart recovery: the two mandated
  full-lifecycle restart tests (PROCESS_ANOMALY and TELEMETRY_FAULT: open → extend
  → **new engine on same DB** → active restored → dedup continues → close → history
  holds exactly one complete lifecycle), multiple active telemetry channels survive
  restart, both categories coexist across restart, deterministic ids don't collide
  after restart, reopen-after-recovery is a new incident across restart, in-memory
  backend retains no cross-instance state, default engine is in-memory/isolated, and
  the backend API serving recovered state after a restart over the same DB.

---

## 9. Limitations

* **SQLite, single node.** Fine for a local MVP; not for concurrent multi-writer or
  high-volume deployments. Documented migration target: PostgreSQL behind the same
  `AlertStore` interface.
* **Best-effort durability on write failure.** Extend/close mutate memory before the
  row write; a write failure there leaves memory momentarily ahead of disk (logged,
  raised). No two-phase commit — deliberately out of scope.
* **History load on startup** reads all rows to rebuild counters/active pointers.
  Trivial at MVP volume; a future store could resume counters via indexed
  `MAX`/`WHERE status='OPEN'` queries instead of a full scan.
* **Timestamps are opaque strings** (as they already were in the `Alert` model);
  the store does not parse or index them as datetimes.
* No change to the 2880 telemetry threshold, TranAD, or the ML threshold.

---

## 10. Future migration path (PostgreSQL / cloud)

Implement a `PostgresAlertStore` satisfying the same `AlertStore` interface (upsert
via `INSERT … ON CONFLICT`, the identical schema, `ORDER BY` an explicit insert
sequence). Select it via `ALERT_STORAGE_BACKEND=postgres` + a DSN. The
`AlertEngine`, monitoring API, and dashboard need **no** change — the whole point of
keeping persistence behind the engine behind one interface. That, plus
containerization, is the natural Phase 8+ direction.
