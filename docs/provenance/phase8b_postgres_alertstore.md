# Phase 8B — PostgreSQL AlertStore

**Status:** complete and smoke-tested locally (SQLite + PostgreSQL).
**Builds on:** Phase 7 (`docs/provenance/phase7_alert_persistence.md`) and Phase 8A
(`docs/provenance/phase8a_docker.md`).
**Scope:** add PostgreSQL as a third interchangeable `AlertStore` backend behind the
existing `AlertEngine`. No change to detection, thresholds, telemetry-health, alert
lifecycle, or the API/dashboard contract. ML artifacts and `threshold.json` are
immutable and untouched.

---

## 1. Motivation

Phase 7 made alerts durable with a single local **SQLite** file — perfect for a
single-node MVP, but a single-writer file that does not model a server/cloud
deployment. Phase 8B demonstrates that the persistence abstraction really is
swappable: the same `AlertEngine`, monitoring API, and dashboard now run unchanged
on a **PostgreSQL** server, the natural precursor to a future multi-instance / cloud
deployment. `memory` and `sqlite` remain fully supported; the default is unchanged.

The store interface was designed for exactly this swap (Phase 7 §10), so the
`AlertEngine` did **not** change: it still owns every lifecycle decision
(open / extend / close / dedup / severity / category). PostgreSQL is a persistence
backend, not a replacement for the engine.

---

## 2. Architecture

```
signals ─▶ AlertEngine (all lifecycle: open/extend/close/dedup/severity/category)
                 │  write-through (whole Alert rows)
                 ▼
        AlertStore  ───────────────────────▶  Monitoring API ─▶ Dashboard
   memory | sqlite | postgres  (no lifecycle logic)   (unchanged surface)
```

* **Engine unchanged.** `AlertEngine` keeps its live working pointers (`_active`,
  `_telemetry_active`) and writes each result through to the store. On construction
  it calls `_recover()` — reading the store once to re-adopt open incidents and
  resume the id counters — exactly as with SQLite.
* **Store does no reasoning.** `PostgreSQLAlertStore` stores and returns whole
  `Alert` records via single-row upserts. Same `AlertStore` protocol as the other
  two backends (`upsert` / `query` / `clear` / `close`).
* **FastAPI stays stateless.** It only drives the engine on `/score` and reads it on
  the monitoring endpoints. `create_app()` selects the store from the environment.
* **`psycopg` is a lazy dependency.** It is imported only inside the PostgreSQL code
  paths, so the `memory` and `sqlite` backends (and the offline unit suite) never
  require it.

---

## 3. PostgreSQL schema

Same *logical* schema as SQLite (Phase 7), expressed in native PostgreSQL types so
an `Alert` round-trips **without changing its API-visible meaning**. One table, one
row per incident (both categories; category-specific columns are nullable):

| column | type | notes |
|---|---|---|
| `seq` | `BIGSERIAL` | insertion order (SQLite used `rowid`); never rewritten on upsert |
| `alert_id` | `TEXT PRIMARY KEY` | deterministic id; conflict target for upsert |
| `category` | `TEXT` | `PROCESS_ANOMALY` / `TELEMETRY_FAULT` |
| `status` | `TEXT` | `OPEN` / `CLOSED` |
| `detected_at`, `window_start`, `window_end`, `opened_at`, `closed_at` | `TEXT` | lifecycle timestamps kept as **opaque strings** (see below) |
| `anomaly_score`, `threshold` | `DOUBLE PRECISION` | ML fields (`0.0` for telemetry) |
| `is_anomaly` | `BOOLEAN` | native boolean |
| `top_features` | `JSONB` | `[[name, error], …]` (empty for telemetry) |
| `window_count` | `INTEGER` | dedup counter |
| `severity` | `TEXT` | heuristic band (placeholder for telemetry) |
| `affected_channels` | `JSONB` | telemetry: `["FIT101"]`; ML: `[]` |
| `reason` | `TEXT` NULL | telemetry cause string |
| `staleness_seconds` | `DOUBLE PRECISION` NULL | telemetry freeze duration |

Indexes on `seq`, `status`, and `category` (the columns the current queries order and
filter by). History order is `ORDER BY seq`; writes use
`INSERT … ON CONFLICT (alert_id) DO UPDATE` (never touching `seq`), so open order is
preserved across updates — identical semantics to the SQLite `rowid` approach.

**Type choices, and why round-trip is exact:**

* **Timestamps stay `TEXT`.** The `Alert` model treats these as opaque strings
  (tests and telemetry use non-datetime values like `"w1"`, `"t1"`), so storing them
  as `TEXT` guarantees an exact round-trip. Mapping to `TIMESTAMP` would change the
  API-visible value and is deliberately avoided.
* **JSON columns become `JSONB`.** A beneficial native type (validated, indexable).
  JSON **arrays preserve order**, so `top_features` and `affected_channels` come back
  in the same order they went in.
* `is_anomaly` → `BOOLEAN`, floats → `DOUBLE PRECISION`, `window_count` → `INTEGER`.

---

## 4. Configuration

`alert_store_from_env()` now supports three backends; the default is unchanged:

| variable | values / meaning | default |
|---|---|---|
| `ALERT_STORAGE_BACKEND` | `memory` \| `sqlite` \| `postgres` | `memory` |
| `ALERT_DB_PATH` | SQLite file path | `alerts.sqlite3` (CWD) — `sqlite` only |
| `ALERT_PG_DSN` | full libpq DSN (wins if set) | — `postgres` only |
| `POSTGRES_HOST` / `POSTGRES_PORT` | server address | `localhost` / `5432` |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | required db/user/password | — (no default) |

A full `ALERT_PG_DSN` wins; otherwise the DSN is assembled from the discrete
`POSTGRES_*` parts using `psycopg.conninfo.make_conninfo` (safe quoting/escaping).
Missing `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` (with no DSN) raises a
clear `AlertStoreError`. Defaulting to `memory` keeps unit tests and any
un-configured caller on the exact pre-Phase-7 behaviour.

---

## 5. Credentials handling

* **No credentials in source.** Nothing is hard-coded; the store receives a
  connection string built from environment variables only.
* **Compose reads them from `.env`** (git-ignored) via `${POSTGRES_*}`
  interpolation. `.env.example` documents the variables with a `change-me`
  placeholder — never a real secret. `POSTGRES_PASSWORD` has **no default** in the
  overlay (`${POSTGRES_PASSWORD:?…}`), so the stack refuses to start without one.
* `.env` is excluded by both `.gitignore` and `.dockerignore` (it never enters an
  image layer).

---

## 6. Startup / readiness

* **Deterministic schema init (MVP).** `PostgreSQLAlertStore.__init__` opens a
  connection pool, waits for the connection (`open(wait=True, timeout=…)`), and runs
  `CREATE TABLE/INDEX IF NOT EXISTS`. An unreachable or misconfigured database
  becomes an immediate, clear `AlertStoreError` rather than a pool silently retrying
  in the background.
* **Compose ordering.** The `postgres` service has a `pg_isready` healthcheck; the
  backend `depends_on: postgres: condition: service_healthy`, so it only starts once
  the database is accepting connections. The backend's own `/health` (TranAD loaded)
  remains its liveness probe.

---

## 7. Restart / recovery semantics

Identical to Phase 7 — the engine's `_recover()` is backend-agnostic. A fresh engine
on the same PostgreSQL database:

1. re-adopts the open `PROCESS_ANOMALY` into `_active`,
2. re-adopts every open `TELEMETRY_FAULT` into `_telemetry_active` (keyed by channel),
3. resumes the id counters (`_seq`, `_tseq`) past every persisted id.

So deduplication and closure continue on the *same* incident, no duplicate incident
or colliding id is minted, and history is never erased. Verified end-to-end for both
categories against a real PostgreSQL server (see §11). **Data survives a PostgreSQL
container restart** via the `postgres-data` named volume.

---

## 8. Transaction / concurrency behaviour

* **Connection pool.** A `psycopg_pool.ConnectionPool` hands each thread its own
  connection, so concurrent access from the threaded TestClient / uvicorn workers is
  safe without a global lock (SQLite used a single locked connection).
* **One transaction per operation.** Each `upsert` / `query` / `clear` runs inside a
  `with pool.connection()` block that commits on clean exit — a single lifecycle step
  is atomic; there is no half-written multi-row state.
* **Parameterised SQL everywhere.** Values are bound (`%(name)s` / `%s`); no value is
  string-interpolated into SQL.
* **Indexes** on `seq` / `status` / `category` back the current ordered/filtered
  reads.
* **No silent success.** A write failure raises `AlertStoreError` (logged with
  context). In the backend, `/score`'s monitoring update stays best-effort (logged
  and swallowed) so scoring is never affected — consistent with Phase 5A/7.
* **Corrupt data is non-fatal.** A single unreadable row (e.g. a JSONB value of the
  wrong shape) is skipped with a warning during load; valid rows still recover.

---

## 9. Docker Compose usage

The base `docker-compose.yml` stays **SQLite-only**, so the default flow is
unchanged:

```bash
# SQLite mode (default, unchanged)
docker compose up --build -d
docker compose down        # keeps the alert-data volume;  down -v wipes it
```

PostgreSQL mode is a **compose overlay** used together with the base file:

```bash
cp .env.example .env                       # then set POSTGRES_PASSWORD
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build -d
# ... use the API (:8000) and dashboard (:8080) exactly as before ...
docker compose -f docker-compose.yml -f docker-compose.postgres.yml down       # keep data
docker compose -f docker-compose.yml -f docker-compose.postgres.yml down -v    # wipe data
```

The overlay adds the `postgres` service (persistent `postgres-data` volume,
`pg_isready` healthcheck, internal-only `expose: 5432` — **not** published to the
host), switches the backend to `ALERT_STORAGE_BACKEND=postgres`, points it at the
`postgres` service over the internal compose network, and makes it wait for a healthy
database. The backend keeps its Phase 8A hardening (non-root, read-only rootfs,
`no-new-privileges`).

---

## 10. Security

* No plaintext credentials in source; secrets come from the environment / `.env`
  only. `.env` is git- and docker-ignored.
* PostgreSQL is reachable only on the internal compose network (`expose`, not
  `ports`) — the backend talks to it in-network; it is not published to the host.
* Persistent database volume (`postgres-data`); no dataset in any image.
* `POSTGRES_PASSWORD` is mandatory (no default) for both services.
* Out of scope (later deployment hardening): TLS to the database, a cloud secret
  manager, a dedicated least-privilege DB role beyond the app user, and production HA.

---

## 11. Test evidence

Baseline before Phase 8B: **386** passing. After: **411** passing (**+25**, no
existing test deleted or weakened; one config test updated because `postgres` is now
a *valid* backend, so its "unknown backend" example changed to `cassandra`).

* `tests/test_postgres_alert_store.py` (**19**) — runs against a **real** PostgreSQL
  server (a throwaway `postgres:16-alpine` Docker container per module, or
  `ALERT_TEST_PG_DSN`; skipped only if neither is available). Covers: construction /
  schema creation / empty start, insert & in-place upsert (no duplicate), open-order
  preserved across updates, `close` idempotency, category/status/limit queries,
  `PROCESS_ANOMALY` and `TELEMETRY_FAULT` field round-trips, `clear`, corrupt-row
  skip, **full-lifecycle restart recovery for both categories**, both categories
  coexisting across restart, id continuity/no-collision across restart, env selection
  (via DSN and via `POSTGRES_*` parts). Invalid-configuration cases (empty conninfo,
  missing credentials, unreachable server) need no server and always run.
* `tests/test_docker_packaging.py` (+6) — the postgres overlay + `.env.example`
  exist, the overlay wires the service/volume/healthcheck and a `service_healthy`
  dependency, commits no secrets and does not publish 5432, `.env.example` has only a
  placeholder, the runtime image ships `psycopg`/`psycopg-pool`, `.dockerignore`
  excludes `.env`, and the base file stays SQLite-only.
* `tests/test_alert_store.py` / `tests/test_alert_persistence.py` — unchanged
  behaviour for the `memory` and `sqlite` backends still passes.

**Real Docker Compose smoke tests (executed locally):**

*A. SQLite mode* — `docker compose up --build -d`: backend + frontend healthy;
`/health` ok; `/score` opens a `PROCESS_ANOMALY`; a telemetry verdict opens a
`TELEMETRY_FAULT`; `/alerts` shows both; `docker compose restart backend` preserves
both alerts (SQLite volume). ✅

*B. PostgreSQL mode* — `docker compose -f docker-compose.yml -f
docker-compose.postgres.yml up --build -d`: postgres reports **healthy**, backend
starts only after it and reports **healthy**; `/health` ok; `/score` opens a
`PROCESS_ANOMALY`; a telemetry verdict opens a `TELEMETRY_FAULT`; `/alerts` exposes
both; **backend restart** preserves both categories; dashboard (`:8080`) serves and
CORS to the API works; **`postgres` container restart** preserves the data via the
`postgres-data` named volume. ✅

*(The exact commands/outputs are recorded in the Phase 8B section of the run.)*

---

## 12. Limitations

* **Single-instance lifecycle only.** PostgreSQL makes the *storage* server-grade,
  but Phase 8B does **not** implement distributed multi-instance `AlertEngine`
  coordination: two engine processes writing the same table would each keep their own
  in-memory active slot (`_active` / `_telemetry_active`), so cross-instance dedup is
  not guaranteed. Making the engine cluster-safe (e.g. `SELECT … FOR UPDATE` on the
  open incident, or a leader) is explicitly out of scope here.
* **Best-effort durability on write failure.** Extend/close mutate memory before the
  row write; a failure there leaves memory momentarily ahead of the database (logged,
  raised). No two-phase commit — deliberately out of scope.
* **History load on startup** reads all rows to rebuild counters/active pointers
  (trivial at MVP volume).
* **Deterministic `CREATE TABLE IF NOT EXISTS` init**, no migration tool yet (see
  §13). No TLS/auth to the database, no HA.
* No change to TranAD, the ML threshold, the 2880 telemetry threshold, or severity
  banding.

---

## 13. Future migration path

Schema initialization is intentionally a single deterministic
`CREATE TABLE/INDEX IF NOT EXISTS` path — the smallest thing that works for one
table. Introducing a migration tool (e.g. **Alembic**) for a single table would add
dependency and indirection without benefit today, so it is deferred. The code is
structured so it can be added cleanly later: the DDL is isolated in `_PG_SCHEMA`, and
a versioned-migration runner could replace the create-if-absent call without touching
the store's read/write API or the engine.

Beyond that: distributed `AlertEngine` coordination for true multi-instance
correctness, TLS + a cloud secret manager, and a managed/HA PostgreSQL — all part of
later deployment hardening (Phase 9+: cloud / IaC / CI-CD).
