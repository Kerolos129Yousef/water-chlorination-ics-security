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

Full test suite: **286 tests passing** (`pytest -q`), including 22 backend API
cases, 15 monitoring-API cases, 12 Phase 3 end-to-end integration cases, and 21
Phase 4 alert-engine cases.

Not started (explicitly out of scope): persistence/database, Docker, AWS,
Terraform, Kubernetes, CI/CD, notifications, authentication.

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
