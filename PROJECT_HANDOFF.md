# Water Chlorination ICS Security — Project Handoff

## Current Status

This repository is the authoritative source of truth for the current graduation project.

Current branch:
main

Phase 8A (Docker containerization), Phase 8B (PostgreSQL AlertStore),
Phase 9 (CI/CD + DevSecOps security gates), Phase 10A (AWS architecture +
Terraform foundation), and Phase 10B (EC2 deployment PREPARATION) are complete
and merged into main.

Latest main commit:
f5c8d5f (Merge pull request #7 from Kerolos129Yousef/phase10b-ec2-deployment)

Phase 10A (AWS architecture + Terraform foundation) is MERGED into main via
PR #6. The infrastructure is DEFINED IN CODE ONLY — it has NOT been deployed:
no `terraform apply` has run, and read-only AWS checks confirm no ics-guardian
VPC, EC2 instance, or state bucket exists. No Docker images were published; no
ECR was introduced.
It adds `infrastructure/terraform/` (validated: fmt clean, validate OK, plan =
12 add / 0 change / 0 destroy with the safe default; 14 once ingress CIDRs are
supplied) and `docs/provenance/phase10a_aws_terraform_foundation.md`. All six
DevSecOps gates were green on the merged commit (a merge-time fix pinned
urllib3 to 2.8.0 and documented two pip-vendored urllib3 HIGH CVEs as
non-reachable Trivy exceptions — see `.trivyignore` and the provenance doc §13).

Phase 10B (EC2 deployment via Docker Hub) is PREPARED and MERGED into main via
PR #7 (code only; NOTHING deployed, no `terraform apply`, no images published,
no state bucket). Read-only AWS checks confirm 0 ics-guardian VPCs/instances,
0 state buckets; both Docker Hub repos are still absent (HTTP 404). It adds a
single web entry point (nginx reverse-proxies
`/api/` to the backend over the private Docker network; the dashboard now uses a
same-origin `/api` base), a dedicated encrypted EBS **data volume** mounted at
`/data` for durable SQLite (survives instance terminate/replace; deleted only by
`terraform destroy`), removes the public port-8000 ingress rule (single public
port 80), adds `deploy/docker-compose.aws.yml` + `deploy/deploy.sh` (SSM-driven,
public Docker Hub images, no secrets), and
`docs/provenance/phase10b_ec2_deployment.md` (full runbook + costs ≈ $24.8/mo).
Owner decisions captured: Docker Hub repos = PUBLIC; exposure = **worldwide HTTP
on TCP 80 only — FORMALLY APPROVED 2026-10-10** (`dashboard_ingress_cidrs =
["0.0.0.0/0"]`, passed explicitly at apply; Terraform default stays `[]`; 8000 &
5432 never public; plain HTTP/no-auth → synthetic data only).
Plan (not applied): 14 add default / 15 add with the approved public port-80
ingress (the sole ingress rule). Remaining before go-live (all deferred, owner
to run): publish the public commit-SHA images (interactive `docker login`),
bootstrap the state bucket, `terraform apply`, deploy via SSM, verify E2E —
exact commands in `docs/provenance/phase10b_ec2_deployment.md` §9.

Publish-step remediation (branch `fix/frontend-trivy-ci`, PR open, NOT merged):
the Step-1 publish was blocked by 41 HIGH + 2 CRITICAL fixable OS CVEs in the
frontend's stale `nginx:1.27-alpine` base. Fixed by moving the frontend to a
maintained `nginx:1.30.5-alpine3.24` base + `apk --no-cache upgrade` (both images
now Trivy-clean: 0 fixable HIGH/CRITICAL), and by extending CI Gate 6 to scan
BOTH production images (it previously scanned only the backend). No findings
suppressed; backend image unchanged. Once merged, publish from the new main SHA.
Next phase after that: Phase 10C (CI/CD deploy automation via GitHub OIDC).

Phase 10A decisions (see the provenance doc for full evidence):
- Architecture: single EC2 + Docker Compose (SQLite), NOT ECS/Fargate/ALB/RDS.
- Region: eu-central-1 (Frankfurt). Reassessed vs me-central-1 (UAE: no T-family,
  only arm64 Graviton — cost + golden-vector FP risk) and me-south-1 (Bahrain:
  has T3/T4g, ~5% pricier, less mature; its region endpoint was unreachable from
  the working env). Frankfurt kept; region is a Terraform variable.
- Ingress is SAFE BY DEFAULT: dashboard_ingress_cidrs defaults to [] so NO public
  80/8000 rule is created until the owner supplies approved CIDRs (SSM-only until
  then). Mixing 0.0.0.0/0 with other CIDRs is rejected by a validation guard.
- Free Tier: corrected. Eligibility depends on account creation date/program
  (pre-2025-07-15 => t2/t3.micro, 12 mo; on/after => t3.micro/small + t4g + flex,
  6 mo + credits). Public IPv4 is NOT "never free" — AWS gives 750 h/mo free for
  eligible accounts. THIS account's eligibility is NOT verified; costs are given
  as a conservative PAID estimate (~$24/mo, assumes no free tier). t3.small is
  free-tier-eligible only under the new program.
- Compute hardening: IMDSv2 required, encrypted gp3, T3 cpu_credits="standard"
  (no surplus-credit charges). SQLite lives on the root volume
  (delete_on_termination=true): stop/start persists, terminate deletes it; NO
  snapshots/backups in 10A — persistence/backup decision deferred to 10B.
- Remote state: S3 backend with native use_lockfile=true (no DynamoDB); the
  state bucket is a separate, not-yet-applied bootstrap config.

Next phase:
Phase 10B — application deployment (build/push images to Docker Hub, deploy the
compose stack to the EC2 instance, wire CORS origin, verify end-to-end).

Working tree should remain clean before starting new work.

---

## Project

Platform for Protecting Water Chlorination Systems from Cyber Attacks

Domain:
OT / ICS Security

Goal:
Detect abnormal/cyber-induced changes in water-treatment process telemetry and surface them as operator-visible incidents.

---

## Completed Phases

Phase 0
Foundation / provenance / environment

Phase 1
Production TranAD inference library

Phase 1.5
Threshold-honest evaluation and provenance

Phase 2A
SWaT replay + RollingWindow

Phase 2B
FastAPI backend

Phase 3
End-to-end integration

Phase 4
AlertEngine

Phase 5A
Monitoring API + operator dashboard

Phase 6A
Telemetry / Sensor Health Hardening

Phase 6B
Telemetry Fault Surfacing / Operator Visibility

Phase 7
Alert Persistence

Phase 8A
Docker containerization (backend + frontend, SQLite via named volume)

Phase 8B
PostgreSQL AlertStore (third interchangeable persistence backend)

Phase 9
CI/CD + DevSecOps security gates (GitHub Actions: lint, tests, SAST,
dependency/secret/container scanning). Validation only — no cloud/IaC/registry.

---

## Current Architecture

SWaT telemetry
→ SWaTReplay
→ RollingWindow + TelemetryHealthMonitor
→ FastAPI /score
→ TranADDetector
→ PROCESS_ANOMALY signal
→ TELEMETRY_FAULT signal
→ AlertEngine
→ AlertStore
→ Monitoring API
→ Operator Dashboard

---

## Architectural Invariants

These are important and must not be violated:

1. RollingWindow owns streaming/window buffering.
2. TelemetryHealthMonitor observes raw telemetry samples.
3. TranADDetector is stateless.
4. FastAPI is thin and stateless.
5. FastAPI must not own per-sample buffering/state.
6. AlertEngine owns incident lifecycle logic.
7. AlertStore provides persistence behind AlertEngine.
8. PROCESS_ANOMALY and TELEMETRY_FAULT are separate semantic categories.
9. Telemetry faults must never be silently converted into ML anomalies.
10. ML input must never be repaired, imputed, suppressed, or modified by telemetry-health logic.
11. ML artifacts are immutable.
12. threshold.json is immutable unless an explicitly approved future phase creates a sibling threshold artifact.
13. Do not retrain TranAD casually.

---

## ML

Actual MVP detector:
TranAD

Model:
30 timesteps × 45 process features

Effective cadence:
5 seconds after subsampling every fifth 1 Hz sample

Temporal context:
approximately 150 seconds

The model reconstructs the final timestep.

Per-feature reconstruction error provides 45-feature attribution.

Current shipped threshold:
9.269035945180804e-05

Primary deployment-oriented metrics:
Precision = 0.7822
Recall = 0.7821
F1 = 0.7822
FPR = 3.0117%
ROC-AUC = 0.9429
PR-AUC = 0.8516

Do NOT use the historical ≈0.9999 threshold-searched F1 as deployment performance.

---

## Dataset

Dataset:
SWaT clean Attack_v0

Facts:
449,919 rows
12.14% row-level attack ratio
approximately 12.15% windowed attack prevalence
35 contiguous attack segments

SWaT CSVs are local/licensed and must not be committed.

---

## Telemetry Health

Phase 6A implemented finite stuck/frozen continuous-channel detection.

Default:
max_unchanged_samples = 2880

At 5-second cadence:
approximately 4 hours

Default monitored set:
25 continuous FIT/LIT/AIT/DPIT/PIT channels

Discrete MV/P/UV channels are excluded by default.

Non-finite values do not count toward a stuck run.

The telemetry-health monitor does NOT alter ML input.

Phase 6A normal SWaT validation produced zero measured telemetry-health false positives on the evaluated normal slice.

This does NOT mean universally zero false positives.

---

## Alert Categories

PROCESS_ANOMALY
- generated by TranAD
- existing ML lifecycle

TELEMETRY_FAULT
- generated by TelemetryHealthMonitor
- per-channel lifecycle
- deduplicated
- closes when the affected channel recovers

Both categories can coexist.

---

## Persistence

Phase 7 implemented AlertStore behind AlertEngine; Phase 8B added a PostgreSQL backend.

Backends:
- InMemoryAlertStore
- SQLiteAlertStore
- PostgreSQLAlertStore (Phase 8B; psycopg 3 + connection pool, JSONB, seq order)

Configuration:
ALERT_STORAGE_BACKEND=memory|sqlite|postgres
ALERT_DB_PATH=...                                  (sqlite)
ALERT_PG_DSN=... OR POSTGRES_HOST/PORT/DB/USER/PASSWORD  (postgres; from env, never source)

Alert persistence includes (all three backends, identical engine semantics):
- PROCESS_ANOMALY
- TELEMETRY_FAULT
- lifecycle state
- history
- ID counters / restart recovery

SQLite runtime files, .env credentials, and datasets must never be committed.

Note: PostgreSQL makes storage server-grade but does NOT add distributed
multi-instance AlertEngine coordination — that is explicitly out of scope for 8B.

---

## Current Tests

Before Phase 7:
351 tests passing

After Phase 7:
381 tests passing

After Phase 8A:
386 tests passing (+5 static Docker-packaging tests)

After Phase 8B:
405 tests passing (+19: real-PostgreSQL integration + docker-packaging for the pg overlay)

Phase 9 (CI/CD) added NO application tests — it wires the existing suite into
GitHub Actions. The current verified count is 411 (the local baseline grew to
411 with dataset + Docker present).

Latest verified suite:
411 passed, 1 warning (local, dataset + Docker present).
CI-like run (no dataset): 16 dataset-gated tests skip; the PostgreSQL suite runs
against a postgres:16-alpine service container via ALERT_TEST_PG_DSN.

The Phase 8B PostgreSQL integration tests run against a real postgres:16-alpine
container (started via Docker locally, or a service container in CI) and are
skipped only if Docker and ALERT_TEST_PG_DSN are both unavailable. The warning
was a pre-existing Starlette/httpx deprecation warning.

When starting new work, ALWAYS run the actual current test suite instead of assuming the old count remains unchanged.

---

## Immutable ML Artifacts

These must remain untouched unless explicitly approved:

ml/artifacts/*
model.pt
threshold.json
scaler.joblib
minmax_scaler.joblib
feature_names.json

No retraining.
No threshold modification.
No artifact replacement.

---

## Documentation

Important current documents include:

PROJECT_CONTEXT.md
README.md

docs/provenance/phase6a_telemetry_health.md
docs/provenance/phase6b_telemetry_fault_alerting.md
docs/provenance/phase7_alert_persistence.md
docs/provenance/phase8a_docker.md
docs/provenance/phase8b_postgres_alertstore.md
docs/provenance/phase9_devsecops.md
docs/provenance/phase10a_aws_terraform_foundation.md

.github/workflows/ci.yml   (the CI/CD pipeline)
.trivyignore               (documented container-scan exceptions)
infrastructure/terraform/  (Phase 10A IaC foundation; validated, NOT applied)

The Phase 1 report is documentation only and must not be modified as part of normal engineering phases unless explicitly requested.

---

## Current Limitations

1. Alert persistence supports memory / SQLite / PostgreSQL behind one interface.
2. PostgreSQL makes storage server-grade, but distributed multi-instance AlertEngine
   coordination is NOT implemented (single engine process assumed).
3. Telemetry-health detection is single-channel focused.
4. No correlated sensor-freeze detection.
5. No advanced drift detection.
6. Current ML FPR is approximately 3%.
7. Threshold remains static.
8. No authentication.
9. No external notifications.
10. No TLS to the database / no cloud secret manager (env/.env only).
11. CI/CD DevSecOps gates exist (Phase 9), but the container scan blocks only on
    FIXABLE HIGH/CRITICAL — unfixed base-OS CVEs are reported, not blocking
    (remediation = hardened base image, Phase 10).
12. Phase 10A Terraform foundation exists (infrastructure/terraform/) and is
    validated, but NOTHING is deployed to AWS yet (no apply has been run).
13. Kubernetes is not implemented.
14. No registry publishing — CI builds and scans images but does not push them.

---

## NEXT PHASE

Phase 10A (AWS architecture + Terraform foundation) is DONE on branch
`phase10a-terraform-foundation` (validated, not applied). Next planned phase:

Phase 10B — application deployment to the selected EC2 architecture
(build/push Docker Hub images, deploy the compose stack, wire the public CORS
origin, verify end-to-end), followed by 10C (CI/CD deploy via GitHub OIDC) and
10D (runtime/IAM hardening).

Phase 10 will also address the Phase 9 follow-ups:
- adopt a hardened/distroless runtime base to drive container HIGH findings toward
  zero and retire the .trivyignore vendored exceptions;
- evaluate torch >= 2.13.0, re-run the golden vectors, and retire the 4 documented
  pip-audit torch exceptions;
- registry publishing of scanned images.

Phase 9 (CI/CD + DevSecOps gates) is complete and validated on GitHub Actions.
Phases 8A (Docker) and 8B (PostgreSQL AlertStore) are complete and smoke-tested.

---

## Planned Roadmap

Phase 8A
Docker + Compose + SQLite

Phase 8B
PostgreSQL AlertStore

Phase 9
CI/CD + DevSecOps security gates  ✅ complete

Phase 10
AWS + Terraform

Phase 11
Kubernetes (stretch)

Later:
observability
performance testing
hardening
notifications
additional telemetry-health improvements
final graduation demonstration/documentation

---

## Critical Rule for Future Agents

The repository is the source of truth.

Do not import architecture or claims from older reports that describe:
- Dense Autoencoder
- dual process/network branches
- WADI production deployment
- SQLite authentication
- Kubernetes already implemented
- Prometheus/Grafana already implemented

Those belong to an older/different project report and must not be treated as current implementation.

Before making changes:
- inspect current code
- inspect current tests
- inspect provenance
- inspect Git state
- verify assumptions

Never change implementation just to satisfy an old document.