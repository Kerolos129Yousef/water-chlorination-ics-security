# Phase 8A — Docker Containerization

**Status:** complete and smoke-tested locally.
**Scope:** reproducible packaging + local deployment of the *current* application.
No functional/semantic change; no PostgreSQL; SQLite remains the persistence
backend. ML artifacts and `threshold.json` are immutable and untouched.

## 1. Goal

Containerize the verified Phase 0–7 application so it builds and runs
reproducibly, CPU-only, non-root, with durable SQLite persistence outside the
image — without changing detection or alert semantics. The container runs the
exact same entrypoint the repo runs locally: `uvicorn backend.app:app`.

## 2. Image architecture

Two images, built from a single build context (the repo root):

| Image | Base | Purpose | Published port |
|-------|------|---------|----------------|
| `ics-guardian-backend:phase8a`  | `python:3.10-slim` (multi-stage) | FastAPI + TranAD detector + AlertEngine + SQLite store | `8000` |
| `ics-guardian-frontend:phase8a` | `nginx:1.27-alpine` | serves the zero-build dashboard (`frontend/index.html`) | `8080` → 80 |

**Backend build is multi-stage:**

* **builder** — creates a self-contained virtualenv at `/opt/venv` and installs
  the pinned runtime deps. `torch` is pulled from the CPU wheel index
  (`https://download.pytorch.org/whl/cpu`), resolving to `torch==2.9.1+cpu`
  (~200 MB vs the ~2.5 GB CUDA default).
* **runtime** — copies only `/opt/venv` + the runtime source. Build toolchain and
  pip caches never reach the final image.

Runtime source copied into the image (nothing else):
`backend/`, `alerting/`, `ml/src/`, `ml/artifacts/swat_TranAD/`.
`ml` stays an implicit namespace package (no `ml/__init__.py`), matching the repo.

Image sizes (local build): backend **1.65 GB** (dominated by CPU torch),
frontend **73.6 MB**.

## 3. Base image & runtime user

* Base: `python:3.10-slim` — matches the verified Python 3.10 environment; minimal.
* Runtime user: non-root `app` (**uid/gid 10001**, fixed for predictable volume
  ownership). Verified: `id` → `uid=10001(app) gid=10001(app)`.

## 4. Startup command & healthcheck

* **Startup:** `uvicorn backend.app:app --host 0.0.0.0 --port 8000` — identical
  module/app to local dev, so no behavioural drift.
* **Healthcheck:** the app's own `GET /health` (200 ⇒ TranAD detector loaded).
  Implemented with stdlib `urllib` so no extra tool is needed in the image:
  `interval=30s timeout=5s start-period=40s retries=3`.
* The frontend has a lightweight `GET /healthz` (nginx `return 200`) for its own
  compose healthcheck.

## 5. Environment variables

Set explicitly in both the image defaults and `docker-compose.yml`:

| Variable | Value | Meaning |
|----------|-------|---------|
| `ALERT_STORAGE_BACKEND` | `sqlite` | durable persistence (Phase 7) |
| `ALERT_DB_PATH` | `/data/alerts.sqlite3` | DB file inside the mounted volume |
| `PYTHONDONTWRITEBYTECODE` | `1` | no `.pyc` writes ⇒ works with a read-only rootfs |
| `PYTHONUNBUFFERED` | `1` | immediate log flushing |

## 6. Volumes & SQLite persistence

* Named volume **`alert-data`** mounted at **`/data`**; the runtime DB lives
  there, *outside the immutable image layer*.
* `/data` is created and `chown app:app` in the image, so the non-root user owns
  the DB. Verified at runtime: `/data/alerts.sqlite3` owned by `app:app`.
* `docker compose down` **keeps** the volume; `docker compose down -v` wipes it.

## 7. Dataset exclusion (production vs research)

* The SWaT CSV dataset is **never** copied into either image. The Dockerfile
  copies only runtime paths, and `.dockerignore` additionally excludes `*.csv`,
  `tests/`, `research/`, `ml/configs/`, etc. as defense-in-depth.
* The production API does **not** need the dataset to start — it loads the small
  immutable artifacts in `ml/artifacts/swat_TranAD/` (model, scalers,
  `feature_names.json`, `threshold.json`; 768 KB total) and serves `/score` on
  caller-supplied windows.
* The **SWaT replay / evaluation** workflow (`simulator/`, `scripts/`) remains a
  separate local/research execution path against the dataset on disk — it is not
  part of the container image.

Verified: `find / -name '*.sqlite3'` in the image → **0**; the only `*.csv`
present are sklearn/numpy's own bundled sample data (no SWaT dataset).

## 8. Local deployment commands

```bash
# build + start backend (:8000) and dashboard (:8080)
docker compose up --build -d

# open the dashboard
xdg-open http://127.0.0.1:8080        # API defaults to http://127.0.0.1:8000

# tail logs / status
docker compose ps
docker compose logs -f backend

# stop (keeps persisted alerts)         # or: down -v to wipe the volume
docker compose down
```

The dashboard is served by nginx on `:8080` and calls the API from the browser.
The backend already CORS-allow-lists `http://localhost:8080` /
`http://127.0.0.1:8080`, so no backend change was needed (verified: a request
with `Origin: http://localhost:8080` to `/status` returns 200).

## 9. Security hardening applied

* Non-root runtime user (uid 10001), `no-new-privileges:true`.
* **Read-only root filesystem** (`read_only: true`); only `/data` (volume) and a
  `/tmp` tmpfs are writable. Verified: `touch /app/x` → *Read-only file system*.
* Minimal slim/alpine bases; multi-stage drops the build toolchain.
* Test-only deps excluded from the runtime image — verified `import pytest` and
  `import httpx` both raise `ModuleNotFoundError` inside the image.
* Pinned dependencies (`docker/requirements-runtime.txt`) for reproducibility.
* No secrets and no dataset baked into the image.
* Application source is not writable at runtime; runtime DB confined to `/data`.
* `.gitignore` keeps the runtime DB (`*.sqlite3*`) and datasets (`*.csv`) out of git.

Out of scope here (Phase 9): CI/CD security gates / DevSecOps scanning.

## 10. Test & smoke evidence

* **Full suite: 386 passed** (baseline **381** + **5** new static Docker-packaging
  tests in `tests/test_docker_packaging.py`). No existing test weakened.
* The packaging tests validate the artifacts without a Docker daemon (CI-safe):
  non-root `USER`, `HEALTHCHECK`/`/health`, multi-stage, no test-only deps,
  `.dockerignore` excludes the dataset, compose wires the SQLite volume + hardening.

**Real Docker Compose smoke test (executed locally):**

1. `docker compose build` → both images built. ✅
2. `docker compose up -d` → backend + frontend both report **healthy**. ✅
3. `GET /health` → `status=ok, detector_loaded=true`. ✅
4. `POST /score` (anomalous window) → `is_anomaly=true` ⇒ **PROCESS_ANOMALY** opened
   (HIGH). ✅
5. `POST /score` with a `telemetry_health` stuck-channel verdict ⇒ **TELEMETRY_FAULT**
   opened (MEDIUM). ✅
6. `GET /alerts` → 2 alerts `[PROCESS_ANOMALY, TELEMETRY_FAULT]`;
   `GET /status` → `active=1 total=2 alert_state_in_memory=false`. ✅
7. `docker compose restart backend` (volume preserved) → container returns healthy;
   `GET /alerts` still returns the **same 2 alerts**, `/status` still
   `total=2, in_memory=false`. **Persistence survives restart.** ✅
8. Dashboard: `GET http://127.0.0.1:8080/` → 200, serves the TranAD Monitor;
   CORS to the API from the `:8080` origin → 200. ✅
9. `docker compose down` → stack stopped cleanly; `alert-data` volume retained. ✅

## 11. Limitations

* SQLite single-writer — fine for the single-backend MVP; PostgreSQL is Phase 8B.
* Backend image ~1.65 GB (CPU torch). Acceptable for now; could shrink later with
  a distroless/`slim` final base or torch trimming — not pursued to avoid risk.
* No TLS / auth / reverse proxy in front (out of scope; local deployment).
* Compose runs one backend replica; horizontal scaling is not addressed here.
* Healthcheck depends on lazy model load completing within `start-period` (40 s);
  generous for the 768 KB artifacts on CPU.

## 12. Recommended Phase 8B

Introduce **PostgreSQL** as an interchangeable `AlertStore` backend behind the
existing `AlertEngine` (the store interface was designed for exactly this swap),
add it as a compose service with its own volume and healthcheck, and select it
via `ALERT_STORAGE_BACKEND=postgres`. Keep SQLite and in-memory supported. Still
no cloud/IaC/CI — that is Phase 9+.
