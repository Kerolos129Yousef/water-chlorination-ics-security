# Project Context

## Project

**Platform for Protecting Water Chlorination Systems from Cyber Attacks**

## Domain

OT / ICS Security

The project focuses on protecting water chlorination systems from cyber attacks using process-aware anomaly detection, monitoring, cloud infrastructure, and DevSecOps practices.

---

## Dataset

**SWaT (Secure Water Treatment) dataset**

SWaT is the primary dataset used for the project's machine learning detection system and MVP.

---

## Selected MVP Model

**TranAD**

TranAD is the **actual anomaly detection model used by the MVP**.

Multiple anomaly detection models were previously trained and evaluated during the research phase. After comparison, TranAD was selected as the model for the MVP.

The project is no longer in the model-selection phase.

### Important

* Do NOT replace TranAD with another ML model.
* Do NOT introduce another model as the MVP detection engine.
* Do NOT retrain TranAD during the MVP integration phase unless explicitly requested.
* Do NOT change the TranAD architecture unless an actual implementation issue requires it and the change is explicitly discussed first.
* The goal is to **integrate and productionize the existing TranAD model**, not to perform another model-selection experiment.

---

## ML Status

The ML experimentation phase has been completed.

Completed:

* SWaT dataset experimentation
* Data preprocessing and experimentation
* Multiple anomaly detection models
* Model training
* Model evaluation
* Model comparison
* TranAD selection
* TranAD trained model artifacts

The existing TranAD artifacts are located at:

```text
ml/artifacts/swat_TranAD/
```

Current artifacts include:

```text
ml/artifacts/swat_TranAD/
├── feature_names.json
├── minmax_scaler.joblib
├── model.pt
├── scaler.joblib
└── threshold.json
```

These artifacts must be treated as the current source of truth for the MVP model.

The exact preprocessing, feature ordering, scaling, windowing, scoring, and threshold logic used during model evaluation must be preserved when building the production inference pipeline.

Do not guess or recreate these values if they can be obtained from the existing research notebooks or artifacts.

---

## Existing Research Code

Research and experimentation notebooks are located under:

```text
research/notebooks/
```

Current structure:

```text
research/
└── notebooks/
    ├── swat/
    │   ├── SWaT_A6_Fusion.ipynb
    │   └── SWaT_Anomaly_Detection.ipynb
    │
    └── wadi/
        └── wadi_console_model_cell.py
```

These files represent the research and experimentation history.

They should NOT be treated as production services.

Do not modify the research notebooks unless explicitly requested.

Production ML code must be separated from research notebooks.

---

## Completed vs Current Goal

### Completed

* Dataset experimentation
* ML experimentation
* Training multiple anomaly detection models
* Model comparison
* TranAD selection
* TranAD model training
* TranAD model artifacts

### Current Goal

Transform the existing validated TranAD research implementation into an end-to-end security monitoring MVP.

The focus is now **software integration and system engineering**, not model selection.

---

# Implementation Status

Current status of the integration/productionization work (as of the latest
closure). This records what is actually built and tested in the repository.

| Phase | Scope | Status |
|---|---|---|
| Phase 0 | Foundation / provenance (verified TranAD/SWaT artifact contract, pinned environment) | **COMPLETE** |
| Phase 1 | TranAD production inference library (`ml/src`) + golden-vector tests | **COMPLETE** |
| Phase 1.5 | Threshold-honesty evaluation, measured detection metrics + provenance | **COMPLETE** |
| Phase 2A | SWaT replay + rolling window (`simulator/`) | **COMPLETE** |
| Phase 2B | FastAPI backend (`backend/`) — thin API over the stateless detector | **COMPLETE** |
| Phase 3 | End-to-end integration: replay → window → HTTP `/score` → TranAD (`simulator/pipeline.py`, `scripts/run_e2e_demo.py`) | **COMPLETE** |
| Phase 4 | Alert engine: detection decisions → deduplicated, lifecycle-managed alerts (`alerting/`) | **COMPLETE** |
| Phase 5A | Monitoring API (`/status`, `/alerts`, `/alerts/active`) + minimal operator dashboard (`frontend/`) | **COMPLETE** |
| Phase 6A | Telemetry / sensor-health hardening: stuck/frozen continuous-channel detection at the ingest layer (`simulator/telemetry_health.py`), distinct from ML anomalies | **COMPLETE** |
| Phase 6B | Telemetry fault surfacing / operator visibility: `TELEMETRY_FAULT` incidents in the `AlertEngine` (second track), monitoring API + dashboard, distinct from and coexisting with `PROCESS_ANOMALY` | **COMPLETE** |
| Phase 7 | Alert persistence: durable `AlertStore` behind the `AlertEngine` (`InMemoryAlertStore` default, `SQLiteAlertStore` opt-in), restart recovery of active incidents + history for both categories | **COMPLETE** |
| Phase 8A | Docker containerization: reproducible CPU-only, non-root, read-only backend image + static dashboard image, `docker-compose` local deployment, SQLite persisted to a named volume (survives restart). No semantic change; no PostgreSQL; dataset excluded from images (`docker/`, `docker-compose.yml`, `docs/provenance/phase8a_docker.md`) | **COMPLETE** |
| Phase 8B | PostgreSQL AlertStore: third interchangeable persistence backend (`PostgreSQLAlertStore`, psycopg 3 + pool) behind the unchanged `AlertStore`/`AlertEngine`; `docker-compose.postgres.yml` overlay (postgres service, named volume, healthcheck, env-only credentials). Memory + SQLite unchanged; no distributed engine coordination (`alerting/store.py`, `docs/provenance/phase8b_postgres_alertstore.md`) | **COMPLETE** |
| Phase 9 | CI/CD + DevSecOps security gates: GitHub Actions pipeline (`.github/workflows/ci.yml`) running six gates on PR→`main` and push→`main` — ruff lint, pytest (with a real `postgres:16-alpine` service; SWaT-gated tests skip), Bandit SAST, pip-audit dependency scan, Gitleaks secret scan, and a real backend Docker build + Trivy image scan. Least-privilege token, SHA-pinned actions, documented fail policy + narrow exceptions (`.trivyignore`, `docs/provenance/phase9_devsecops.md`). Validation only — no cloud/IaC/registry; no ML/threshold/dataset change | **COMPLETE** |

**Actual implementation order:** Phase 2A (SWaT replay + rolling window) was
built **before** Phase 2B (FastAPI backend). This differs from the numbered
progression sketched under *Development Strategy* (which listed the backend
before telemetry replay); the replay + rolling-window layer was implemented
first so the backend could be designed as a thin, stateless API with the
30-sample buffering owned entirely by the caller (`RollingWindow`).

**Key architecture decisions in force:**

* TranAD is the actual MVP detection model; the five artifacts under
  `ml/artifacts/swat_TranAD/` are treated as immutable / byte-identical.
* `TranADDetector` is **stateless** and requires exactly a `30 × 45` input.
* The rolling 30-sample buffer is owned by `simulator.window_buffer.RollingWindow`,
  **not** by FastAPI. The backend keeps no per-request/streaming state.
* The FastAPI layer is a **thin** boundary over `ml/src` (validate → delegate →
  shape response); dependency direction is strictly `backend → ml.src`.
* **Threshold recalibration: DEFERRED.** The current `threshold.json` (99th
  percentile) is used as-is; the known FPR caveat is surfaced, not silently
  corrected.

**Phase 3 wires the layers end-to-end** over the FastAPI HTTP boundary
(`SWaTReplay → RollingWindow → POST /score → TranADDetector`) via an *injected*
scorer, so no boundary is violated: buffering stays in `RollingWindow`, scoring
stays behind the API, and `simulator/` still imports neither `torch`, `fastapi`,
nor `ml.src`. Verified on real SWaT data — clean segments stay normal (0 false
positives on the sampled windows), attack segments are flagged (100% on the
demo's auto-selected segment), over both the in-process ASGI app and a real
`uvicorn` socket.

**Phase 4 adds the alert engine** (`alerting/`): the one intentionally *stateful*
stage. It turns per-window detector decisions into deduplicated, lifecycle-managed
alerts (a continuous anomaly is ONE alert: `OPEN` → dedup across overlapping
windows → `CLOSE` on return to normal). It duck-types on the detector's existing
output, so it imports no `torch`, FastAPI, `ml.src`, or `simulator`; attribution
is reused verbatim (never recomputed); severity is an explicitly-labelled
engineering heuristic (`score/threshold` ratio), not an ML prediction. In-memory
only — no database yet.

**Phase 5A makes the MVP observable**: the backend gains read-only monitoring
endpoints (`GET /status`, `/alerts`, `/alerts/active`) over the in-memory
`AlertEngine` held on `app.state` and fed from the `/score` flow (the `/score`
response contract is unchanged; dedup/lifecycle/severity/attribution stay in
`alerting/`). A minimal zero-build operator dashboard (`frontend/index.html`,
vanilla HTML/JS, polling) consumes only the API. Explicit dev CORS
(`DEV_CORS_ORIGINS`, not a wildcard). **Alert state is in-memory only and is lost
on backend restart — no database in this phase (deferred, to be evaluated with
persistence).**

**Phase 6A adds telemetry / sensor-health hardening**: a `TelemetryHealthMonitor`
(`simulator/telemetry_health.py`) at the ingest layer, alongside `RollingWindow`,
that detects *finite-but-stuck* continuous sensor channels (a value bit-identical
for `max_unchanged_samples` consecutive samples). It resolves provenance §7 open
item 4. Deliberately separate from TranAD: it is read-only (never forward-fills,
repairs, or suppresses the ML input), imports only numpy, and emits an explicit
`TELEMETRY_FAULT` / `STUCK_CHANNEL` signal that stays distinguishable from an ML
`PROCESS_ANOMALY` (both can be present on one window). Only continuous sensors
(`FIT/LIT/AIT/DPIT/PIT`) are monitored by default; discrete actuators (`MV/P/UV`)
legitimately hold state and are excluded. The default `max_unchanged_samples=2880`
(4 h at the 5 s cadence) is **measured zero-false-positive** on the normal SWaT
slice (see `docs/provenance/phase6a_telemetry_health.md`). By design this is NOT
wired into the thin, stateless FastAPI layer — per-sample stream state stays at
the ingest layer. `run_pipeline` accepts an optional `health_monitor` and carries
the verdict on `EndToEndResult.telemetry_health`.

**Phase 6B surfaces telemetry faults to operators** without changing the detector.
The `AlertEngine` gains a *second, independent track*: `process_health()` turns a
`TelemetryHealthResult` into deduplicated, lifecycle-managed `TELEMETRY_FAULT`
incidents (one per stuck channel; open on staleness, dedup while stuck, close on
the deterministic recovery rule). This is kept semantically separate from
`PROCESS_ANOMALY` — the two can coexist on the same period and never merge. The
Phase 4 ML single-active-slot machine is unchanged; FastAPI stays stateless (the
client attaches the per-window health verdict to the existing `/score` request and
the backend only *forwards* it to the engine). The monitoring API adds a `/status`
telemetry rollup, a `category` filter on `/alerts`, and telemetry fields on
`AlertModel`; `/alerts/active` stays ML-only. The dashboard adds a telemetry-faults
card + history and a system-status rollup, in the existing visual language. Alert
state remains **in-memory only** (no persistence). Detection threshold unchanged
(2880). See `docs/provenance/phase6b_telemetry_fault_alerting.md`.

**Phase 7 makes alert state durable** without changing any lifecycle semantics. A
small `AlertStore` interface sits *behind* the `AlertEngine`: `InMemoryAlertStore`
(default — identical to prior behaviour) and `SQLiteAlertStore` (opt-in, a single
local file, no server). The engine still owns every lifecycle decision and now
writes each result through to the store; on construction it recovers the active
`PROCESS_ANOMALY` slot, the per-channel `TELEMETRY_FAULT` incidents, the combined
history, and the id counters — so a restart resumes the exact lifecycle, minting no
duplicate incidents or ids. FastAPI stays stateless (persistence is entirely behind
the engine). Configured via `ALERT_STORAGE_BACKEND=memory|sqlite` +
`ALERT_DB_PATH`; runtime `*.sqlite3` files are git-ignored. API/dashboard contracts
are byte-compatible. SQLite is the MVP backend and is replaceable by PostgreSQL
behind the same interface with no engine change. See
`docs/provenance/phase7_alert_persistence.md`.

**Phase 8A packages the current application in Docker** with no functional change.
A multi-stage `python:3.10-slim` backend image runs the same
`uvicorn backend.app:app` CPU-only as non-root (uid 10001) on a read-only root
filesystem; only the `alert-data` named volume (`/data`) and a `/tmp` tmpfs are
writable, so the SQLite DB (`ALERT_DB_PATH=/data/alerts.sqlite3`,
`ALERT_STORAGE_BACKEND=sqlite`) lives outside the immutable image and survives
container restarts. A tiny `nginx:alpine` image serves the unchanged zero-build
dashboard on `:8080` (the backend already CORS-allows that origin). The SWaT
dataset is never baked into any image; the production API starts from the 768 KB
immutable artifacts alone, while SWaT replay/evaluation stays a separate research
path. `docker compose up --build` runs the stack locally. See
`docs/provenance/phase8a_docker.md`.

**Phase 8B adds PostgreSQL as a third interchangeable `AlertStore` backend** behind
the unchanged `AlertEngine`. `PostgreSQLAlertStore` (`alerting/store.py`) uses
psycopg 3 with a connection pool and the same logical schema as SQLite in native
types (JSONB for the list columns, `seq BIGSERIAL` for open order, BOOLEAN/DOUBLE
PRECISION, indexed on `seq`/`status`/`category`); each op is one parameterised,
autocommitted transaction. `psycopg` is imported lazily, so the memory/sqlite paths
never require it. Selection is `ALERT_STORAGE_BACKEND=memory|sqlite|postgres` with
`ALERT_PG_DSN` or `POSTGRES_HOST/PORT/DB/USER/PASSWORD` (credentials from the
environment only, never source). A `docker-compose.postgres.yml` overlay (used with
the SQLite-only base file) adds a `postgres` service with a persistent named volume,
`pg_isready` healthcheck (backend waits for it), internal-only networking, and
`.env`-supplied credentials. Restart recovery, id continuity, and both alert
categories work identically to SQLite (the engine's `_recover()` is
backend-agnostic). Explicitly **out of scope for 8B**: distributed multi-instance
`AlertEngine` coordination — PostgreSQL makes storage server-grade, but two engine
processes would each keep their own in-memory active slot. See
`docs/provenance/phase8b_postgres_alertstore.md`.

Full test suite: **411 tests passing** (`pytest -q`) — the 386 Phase 0–8A cases
plus 25 Phase 8B cases: 19 real-PostgreSQL integration tests against a throwaway
`postgres:16-alpine` container (construction/schema, upsert/query, both-category
full-lifecycle restart recovery, id continuity, env selection, invalid-config
handling) and 6 docker-packaging checks for the postgres overlay. Both persistence
modes were exercised with **real `docker compose` smoke tests**: SQLite mode and
PostgreSQL mode each verified end-to-end (health → `/score` → both alert categories
persisted across a backend restart → dashboard reachable), and in PostgreSQL mode
the data additionally survived a **postgres container restart** via the named volume.

**Phase 9 wires the existing suite and image build into a GitHub Actions
DevSecOps pipeline** (`.github/workflows/ci.yml`, `docs/provenance/phase9_devsecops.md`)
with six gates — lint, tests (real PostgreSQL 16 service; SWaT-gated tests skip),
Bandit SAST, pip-audit, Gitleaks, and a real Docker build + Trivy scan. It adds no
application tests and changes no ML/threshold/dataset material; in CI the 16
dataset-gated tests skip and the PostgreSQL suite runs against a service container.

Not started (explicitly out of scope): distributed multi-instance coordination,
cloud persistence, AWS, Terraform, Kubernetes, registry publishing, notifications,
authentication, TLS to the database.

---

# MVP Architecture

The target MVP flow is:

```text
SWaT Telemetry
      ↓
Preprocessing
      ↓
TranAD
      ↓
Anomaly Score
      ↓
Threshold / Detection Decision
      ↓
Alert Engine
      ↓
FastAPI Backend
      ↓
Monitoring Dashboard
```

The MVP should demonstrate the complete path from SWaT telemetry to anomaly detection and operator-visible alerting.

---

## MVP Components

### 1. SWaT Telemetry

Provide SWaT data to the system in a way that can represent incoming telemetry.

For the MVP, a replay/simulation mechanism can be used to feed historical SWaT data through the system.

### 2. Preprocessing

Implement the exact preprocessing pipeline required by the existing TranAD model.

The production preprocessing implementation must remain consistent with the preprocessing used during model evaluation.

### 3. TranAD

TranAD is the central anomaly detection engine.

It must load the existing trained artifact:

```text
ml/artifacts/swat_TranAD/model.pt
```

and use the corresponding preprocessing/scaling and threshold artifacts.

### 4. Anomaly Detection

The system should produce an anomaly score and determine whether the current telemetry represents an anomaly according to the existing threshold logic.

A conceptual output may contain:

```json
{
  "is_anomaly": true,
  "anomaly_score": 0.87,
  "threshold": 0.65,
  "timestamp": "...",
  "severity": "high"
}
```

The exact scoring and threshold behavior must be based on the existing TranAD implementation rather than guessed values.

### 5. Alert Engine

Convert detected anomalies into structured security/process alerts.

Alerts should contain useful information such as:

* Timestamp
* Anomaly score
* Threshold
* Detection status
* Severity
* Relevant telemetry/features when available

### 6. FastAPI Backend

Expose the detection and monitoring functionality through a backend API.

The backend should provide a clean interface between the ML detection layer and the dashboard.

### 7. Monitoring Dashboard

The dashboard should provide an operator-oriented view of:

* Current process/telemetry status
* Anomaly detection status
* Anomaly scores
* Threshold
* Security/process alerts
* Alert history
* Model status

---

# Attack / Anomaly Demonstration

A major MVP objective is to demonstrate the system detecting abnormal behavior using SWaT data.

The desired demonstration flow is:

```text
SWaT Normal Data
       ↓
Telemetry Replay
       ↓
TranAD
       ↓
Normal
```

followed by an attack/anomalous scenario:

```text
SWaT Attack Scenario
       ↓
Telemetry Replay
       ↓
TranAD
       ↓
Anomaly Score ↑
       ↓
Detection
       ↓
Alert
       ↓
Dashboard
```

The final project should demonstrate the detection capability using the available labelled SWaT attack scenarios.

---

# Post-MVP Scope

The following capabilities are part of the broader project and should be implemented **after the local MVP is working**:

* Docker
* AWS cloud deployment
* Terraform
* Kubernetes
* CI/CD
* Security scanning
* DevSecOps security gates
* End-to-end testing
* Performance tuning
* Final attack demonstration
* Technical documentation

The project proposal identifies cloud infrastructure, Docker/Kubernetes, and CI/CD with integrated security gates as part of the broader system architecture.

---

# Development Strategy

Development should happen incrementally.

The recommended progression is:

```text
Phase 1
TranAD Inference Module
        ↓
Phase 2
FastAPI Backend
        ↓
Phase 3
SWaT Telemetry Replay
        ↓
Phase 4
Alert Engine
        ↓
Phase 5
Monitoring Dashboard
        ↓
Phase 6
End-to-End Local MVP
        ↓
Phase 7
Docker
        ↓
Phase 8
AWS Infrastructure
        ↓
Phase 9
CI/CD + DevSecOps Security Gates
        ↓
Phase 10
Kubernetes
        ↓
Phase 11
Testing / Validation / Performance
        ↓
Phase 12
Final Demonstration + Documentation
```

---

# Important Engineering Constraints

## Preserve Existing ML Work

The existing TranAD model is considered completed research work.

Do not unnecessarily rewrite it.

The goal is to wrap and integrate the existing implementation into maintainable production-oriented components.

## Separate Research from Production

Research notebooks:

```text
research/notebooks/
```

Production ML:

```text
ml/src/
```

Do not directly import notebooks into the production application.

## Preserve Model Compatibility

The production inference pipeline must be compatible with:

```text
model.pt
feature_names.json
scaler.joblib
minmax_scaler.joblib
threshold.json
```

The Agent must inspect the existing implementation before making assumptions about how these artifacts are used.

## No Premature Cloud Infrastructure

Do not introduce AWS, Kubernetes, Terraform, or CI/CD before the local MVP is functional unless explicitly requested.

The local end-to-end MVP should work first.

> **Status update:** this gate has been satisfied. The local MVP, Docker (8A/8B),
> and CI/CD + DevSecOps (9) are complete, so cloud work is now authorized and
> under way. **Phase 10A** (AWS architecture + Terraform foundation) is
> implemented on branch `phase10a-terraform-foundation`: `infrastructure/terraform/`
> is written and validated but **NOT applied to AWS**. Selected design: a single
> EC2 instance running the existing Docker Compose stack (SQLite), in
> `eu-central-1` (Frankfurt) — not ECS/Fargate/ALB/RDS, and not `me-central-1`
> (UAE has no T-family instance; only arm64 Graviton, a golden-vector FP risk).
> `me-south-1` (Bahrain) was also evaluated (has T3/T4g, slightly pricier, less
> mature) — Frankfurt kept. Region/size are Terraform variables. **Public ingress
> is safe-by-default** (no 80/8000 rule until the owner supplies CIDRs). Free-Tier
> eligibility for THIS account could not be verified (depends on account creation
> date/program); costs are given as a conservative PAID estimate (~$24/mo running
> 24/7, assuming no free tier). Full evidence:
> `docs/provenance/phase10a_aws_terraform_foundation.md`.

## Testing

Every major implementation phase must include appropriate tests.

At minimum, the system should eventually have:

* Unit tests
* Integration tests
* ML inference tests
* API tests
* End-to-end tests

## Small Incremental Changes

Prefer small, reviewable changes over large rewrites.

Do not modify unrelated parts of the repository.

Do not introduce unnecessary dependencies.

Do not change the architecture without explaining the reason first.

---

# Development Workflow

For every major phase:

1. Inspect the existing implementation and relevant artifacts.
2. Explain the proposed changes.
3. Identify assumptions and unknowns.
4. Implement the smallest coherent change.
5. Add or update tests.
6. Run the tests.
7. Verify the application.
8. Update documentation where necessary.
9. Summarize what changed.
10. Identify remaining issues before moving to the next phase.

Do not automatically proceed to the next major phase.

---

# Agent Rules

The coding agent must behave as a senior software engineer and ML/DevSecOps engineer.

Before modifying existing ML code:

* Inspect the relevant notebooks.
* Inspect the model artifacts.
* Understand the current preprocessing.
* Understand feature ordering.
* Understand windowing.
* Understand anomaly scoring.
* Understand threshold calculation/use.
* Confirm compatibility with the saved TranAD model.

If something is unclear:

**Do not guess. Inspect the existing implementation first.**

If a major architectural decision is required:

**Explain the decision and its trade-offs before implementing it.**

---

# Current Priority

The immediate priority is:

```text
Existing TranAD artifacts
        ↓
Understand existing TranAD inference logic
        ↓
Create clean production inference module
        ↓
Test inference
        ↓
Expose through FastAPI
```

Do NOT start with AWS, Kubernetes, Terraform, CI/CD, or frontend implementation.

The first objective is to make the existing TranAD model reliably usable as a production-oriented inference component.

---

# Definition of MVP Completion

The MVP is considered functional when the following end-to-end flow works locally:

```text
SWaT telemetry
      ↓
Preprocessing
      ↓
Existing TranAD model
      ↓
Anomaly score
      ↓
Threshold decision
      ↓
Alert generation
      ↓
FastAPI
      ↓
Monitoring Dashboard
```

and the system can demonstrate detection of an anomalous/attack scenario from SWaT data.

The MVP must use **TranAD as its actual detection model**.

---

# Project Principle

The goal is not simply to build an ML model.

The goal is to turn the validated TranAD detection capability into a complete OT/ICS security monitoring product.

The system should combine:

**OT/ICS security + process-aware anomaly detection + software engineering + cloud + DevSecOps.**
