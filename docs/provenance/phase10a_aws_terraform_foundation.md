# Phase 10A — AWS Architecture, Terraform Foundation & Cost-Safe Infrastructure

**Status:** Terraform code **written and validated only**. **Nothing has been
applied to AWS.** No resources were created, changed, or destroyed. No images
were published. This document is the authoritative record of the Phase 10A
design decisions and the exact procedure for a later, owner-approved deployment.

- **Branch:** `phase10a-terraform-foundation`
- **Base commit (verified Phase 9 HEAD):** `9d73cb31a77824e654940094c84d26dc8c69bb95`
- **Region selected:** `eu-central-1` (Frankfurt) — see §3 for why *not* `me-central-1`.
- **Architecture selected:** Option A — single EC2 + Docker Compose (SQLite).

---

## 1. Application architecture (what we are deploying)

Verified by inspecting the repo (not assumed):

| Component | What it is | Port | State |
|-----------|-----------|------|-------|
| `backend` | FastAPI/uvicorn over the stateless TranAD detector (`docker/backend.Dockerfile`) | 8000 | holds alert lifecycle in the configured `AlertStore` |
| `frontend` | nginx serving a single static dashboard `frontend/index.html` (`docker/frontend.Dockerfile`) | 80 | none |
| `postgres` *(optional)* | `postgres:16-alpine` overlay (`docker-compose.postgres.yml`) | 5432 (internal only) | durable volume |

Key facts that shaped the design:

- The dashboard talks to the backend **from the browser** (client-side `fetch`),
  so the backend port must be reachable by the client — it is **not** a purely
  internal port. CORS is allow-listed to `localhost:5500/8080` only
  (`backend/app.py`); wiring a real public origin is a **Phase 10B** app-config
  task and is **not** changed here.
- The backend is **stateless** except for alert persistence. Default persistence
  is **SQLite on a named Docker volume** (`docker-compose.yml`); PostgreSQL is an
  optional overlay. Only the alert history is stateful — the model and inference
  are immutable and reproducible.
- The image is CPU-only `torch==2.9.1` pinned for **golden-vector reproducibility**
  (`docker/requirements-runtime.txt`). This pin, and the existing
  Intel-vs-AMD FP sensitivity of the golden tests (`atol=1e-9`, see
  `phase9_devsecops.md`), make the **CPU architecture a correctness concern**:
  moving to arm64 is a golden-vector risk and is avoided (§3).

---

## 2. Architecture options compared

| Dimension | **A. EC2 + Docker Compose** *(selected)* | B. ECS/Fargate + ALB + RDS | C. EC2 + RDS (hybrid) |
|---|---|---|---|
| Compute | 1× `t3.small` | Fargate task(s) | 1× `t3.small` |
| Load balancer | none (direct IP) | public ALB (~$19/mo always-on) | none |
| Database | SQLite on gp3 volume | RDS PostgreSQL (private) | RDS PostgreSQL |
| Outbound for image pulls | IGW (free) | NAT GW (~$35/mo) **or** public tasks | IGW |
| Est. recurring (24/7) | **~$24/mo** (see §8) | **~$70–110/mo** | ~$40/mo |
| Private subnets / NAT | none needed | needed (cost) | none |
| Secrets for Docker Hub | runtime login on host | Secrets Manager + `repositoryCredentials` | runtime login |
| Operational burden | low (one box) | high (task defs, roles, target groups, ALB, RDS) | medium |
| Persistence / recovery | EBS volume + snapshots; redeploy app from image | managed RDS backups | managed RDS backups |
| Portfolio value | strong: real AWS + Terraform + Docker + SSM + IAM + remote state | strongest but overkill & costly | medium |
| Fits "cost-conscious graduation demo" | **best** | no (recurring ALB+NAT+RDS even idle) | partial |

**Decision — Option A.** It meets the actual graduation-project need (a real,
secure, reproducible single-node cloud deployment of the existing app), gives
genuine AWS/Terraform/Docker/DevSecOps experience, and avoids the always-on ALB
+ NAT + RDS charges that Option B incurs **even with zero traffic**. ECS/Fargate,
ALB, and RDS were explicitly **not** chosen merely for looking production-grade.

---

## 3. Region decision — `eu-central-1`, NOT `me-central-1`

`me-central-1` (UAE) was the initial candidate. Read-only AWS API inspection
(account alias `kyz-v2`, region enabled) produced **decisive evidence against it**:

1. **No free-tier-eligible instance exists in `me-central-1`.**
   `describe-instance-type-offerings` returns **no T-family** (`t2/t3/t3a/t4g`)
   types at all. The smallest general-purpose types are **arm64 Graviton**
   `m6g.medium`/`m7g.medium` (4 GiB) at **$0.0473 / $0.0500 per hr** (~$34–37/mo).
2. **arm64 is a golden-vector risk.** The cheapest UAE instances are Graviton
   (arm64). The golden tests are already FP-sensitive across x86 vendors; an
   arch change is an unnecessary correctness hazard. This project must preserve
   inference semantics (immutable ML artifacts).
3. **No verifiable free tier anyway** (§8): even if it mattered, UAE could never
   be free-tier-covered because it lacks the eligible instance types.

`eu-central-1` (Frankfurt) is the **closest full-service region to the UAE** that
offers free-tier-eligible **x86** types (`t3.micro` $0.012/hr, `t3.small`
$0.024/hr), 3 AZs, and the AL2023 x86_64 AMI. Region remains a **Terraform
variable** (`var.region`), so the owner can override it; the default was changed
with documented evidence rather than silently.

> If UAE data-residency is later required, the trade-off is ~$36+/mo on arm64
> plus re-verifying the golden vectors on Graviton before trusting the model.

---

## 4. Selected infrastructure (what the Terraform creates)

Region `eu-central-1`, AZ chosen from a data source (not hard-coded):

- **Networking** (`modules/network`): one VPC (`10.20.0.0/24`), one **public**
  subnet (`/28`), Internet Gateway, public route table + default route. **No**
  private subnets, **no NAT Gateway**, **no ALB** — not needed for one node.
- **Security group**: inbound **80** (dashboard) and **8000** (API) only, each
  restricted to `var.dashboard_ingress_cidrs` (default `0.0.0.0/0` for a public
  portfolio demo; set to your `/32` to lock it down). **No inbound 22. No
  inbound 5432.** Egress all (Docker Hub pulls, OS updates, SSM).
- **Compute** (`modules/compute`): `t3.small` (x86_64, 2 GiB), AL2023 AMI
  resolved live from the SSM public parameter, **IMDSv2 required**, **encrypted
  gp3** 30 GiB root. `user_data` installs Docker + compose plugin and enables the
  daemon — it makes the node **ready**; it does **not** pull or run the app.
- **IAM**: an instance role whose **only** policy is the AWS-managed
  `AmazonSSMManagedInstanceCore` (least privilege; enables Session Manager, no
  SSH). **No admin, no wildcards.**

Resource plan: **14 to add, 0 to change, 0 to destroy** (§7).

---

## 5. Docker Hub registry strategy

- Registry = **Docker Hub** (account `kerolosyousef`). **No ECR.** No images
  pushed, no repos created or made public/private in this phase.
- Existing image names (from compose): `ics-guardian-backend:phase8a`,
  `ics-guardian-frontend:phase8a`. **Phase 10B** will decide the published
  repository names/tags (e.g. `kerolosyousef/ics-guardian-backend:<version>`),
  build, tag, push, and pull them on the instance.
- **Credentials stay out of Terraform, Dockerfiles, images, user-data, and git.**
  If repos are **public**, the instance pulls with no credentials. If **private**,
  the Phase 10B approach is: store a Docker Hub **access token** in AWS Secrets
  Manager and grant the instance role a **narrowly-scoped** `secretsmanager:GetSecretValue`
  on that one secret, then `docker login` at deploy time. That IAM statement is
  added in 10B/10D — not pre-created here.

---

## 6. Network boundaries & security-group flow

```
Internet --:80---> nginx dashboard (static)         |  both on the
Internet --:8000-> FastAPI backend (browser fetch)  |  single EC2 node
Operator --SSM Session Manager--> shell              (no inbound 22; egress 443 to SSM)
backend  --Docker network--> postgres:5432           (only if pg overlay used; never exposed)
EC2      --egress 443/80--> Docker Hub, OS mirrors, SSM endpoints (via IGW)
```

Hardening already encoded: IMDSv2 required, encrypted EBS, SSM instead of SSH,
least-privilege instance role, DB port never in the security group. Dropping the
public `8000` behind an nginx reverse proxy (single port 80/443) is a **10B/10D**
option noted for later; it requires an app/proxy change, so it is not done here.

---

## 7. Validation performed (no AWS changes)

| Check | Result |
|---|---|
| `terraform fmt -recursive` | clean (no diffs) |
| `terraform validate` (dev) | **Success! The configuration is valid.** |
| `terraform validate` (bootstrap) | **Success! The configuration is valid.** |
| `terraform plan` (local backend, read-only) | **Plan: 14 to add, 0 to change, 0 to destroy** |
| AWS provider resolved | `hashicorp/aws v6.68.0` (locked in `.terraform.lock.hcl`) |
| Ruff gate (`E9,F`) | All checks passed |
| Bandit gate (sev≥medium, conf≥high) | no findings (exit 0) |
| Test suite (`pytest -q`) | **411 passed, 1 pre-existing warning** (unchanged baseline) |
| Protected ML artifacts / golden fixtures | **unchanged** (git shows only `.gitignore` + `infrastructure/`) |

The `plan` was produced with a **temporary local backend override** so no S3
state bucket was created; the override and its local state were removed
afterwards. The 14 planned resources: VPC, IGW, subnet, route table, route,
route-table association, security group, 3 SG rules, IAM role, role-policy
attachment, instance profile, EC2 instance.

---

## 8. Cost model & Free-Tier reality

**Free-Tier eligibility could NOT be positively verified for this account.**
The Free Tier API (`freetier get-free-tier-usage`) returned **only "Always Free"**
entries (SNS/SQS/Glue/KMS) and **zero "12 Months Free"** records. This does not
prove the 12-month EC2/EBS free tier is absent, but there is **no evidence it is
active**, and the owner must confirm in the Billing console. **Per project policy
we therefore cost everything as PAID** and do not claim any EC2/EBS is free.

Recommended deployment, `eu-central-1`, running 24/7 (verified unit prices):

| Item | Unit price | Monthly (730 h) |
|---|---|---|
| EC2 `t3.small` (on-demand, Linux) | $0.0240/hr | **$17.52** |
| EBS gp3 root, 30 GiB | $0.0952/GB-mo | **$2.86** |
| Public IPv4 address (in-use) | $0.005/hr | **$3.65** |
| S3 remote state (tiny, versioned) | ~usage | **~$0.01** |
| Data transfer out | 100 GB/mo free, then $0.09/GB | **~$0** (demo) |
| **Total (always-on)** | | **≈ $24 / month** |

Cost-control notes:

- **Billed continuously while running:** EC2 compute, the public IPv4 address.
- **Billed even when stopped:** the **EBS volume** (~$2.86/mo). Stopping the
  instance releases the auto-assigned public IPv4 (no IPv4 charge while stopped).
  → **Stop the instance between demos** to drop to ~$3/mo.
- **If the 12-mo free tier *is* active** and `t3.micro` is used: 750 h compute
  and 30 GB EBS could be \$0, but the **public IPv4 (~$3.65/mo) is never free**.
- **No** EKS/WAF/CloudFront/Route 53/ElastiCache/OpenSearch/NAT/multi-region/ALB
  is created — each avoided on cost grounds.

Rejected Option B would add always-on **ALB (~$19/mo)** + **NAT (~$35/mo)** +
**RDS (~$13+/mo)** ⇒ **~$70–110/mo even idle** — the main reason it was declined.

---

## 9. Terraform structure, state & bootstrap

```
infrastructure/terraform/
├── README.md
├── bootstrap/                 # one-time S3 state bucket (LOCAL state); not applied in 10A
├── modules/network/           # VPC, subnet, IGW, routes, security group
├── modules/compute/           # IAM role (SSM-only), instance profile, EC2 + gp3
└── environments/dev/          # root: providers, S3 backend, locals/tags, module wiring, outputs
```

- Provider pinned `hashicorp/aws ~> 6.0`; `required_version >= 1.11.0, < 2.0.0`;
  `.terraform.lock.hcl` committed for `dev` and `bootstrap`.
- Consistent tags via provider `default_tags` (`Project`, `Environment`,
  `ManagedBy`) + `locals.name_prefix` (`ics-guardian-dev`).
- **Remote state:** S3 backend with **native `use_lockfile = true`** locking.
  **No DynamoDB.** The backend `bucket` is supplied at `init` time
  (`-backend-config`) so no account-specific name is committed.
- **Bootstrap sequence (run later):** `bootstrap/` creates the state bucket
  (versioned, SSE-AES256, all public-access blocked, TLS-only policy,
  `prevent_destroy`) with **local** state, because a backend bucket cannot store
  the state of the run that creates it.

### Exact commands for a later, owner-approved deployment

```bash
# 0) Phase 10A re-validation (no AWS changes)
terraform -chdir=infrastructure/terraform/environments/dev fmt -check -recursive
terraform -chdir=infrastructure/terraform/environments/dev init -backend=false
terraform -chdir=infrastructure/terraform/environments/dev validate

# 1) Create the remote-state bucket (ONCE)
cd infrastructure/terraform/bootstrap
terraform init
terraform apply -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'

# 2) Initialise dev against that bucket
cd ../environments/dev
terraform init \
  -backend-config="bucket=ics-guardian-tfstate-<ACCOUNT_ID>" \
  -backend-config="region=eu-central-1"

# 3) REVIEW the plan (cost + exposure), then apply
terraform plan -out=plan.tfplan
terraform apply plan.tfplan
```

### Teardown

```bash
cd infrastructure/terraform/environments/dev
terraform destroy      # VPC, instance, EBS, IAM role/profile, SG
# bootstrap bucket is retained (prevent_destroy); empty + unguard it only when
# retiring the whole project.
```

---

## 10. Deferred to Phase 10B / 10C / 10D

- **10B:** choose/publish Docker Hub repo names, build + push images, deploy the
  compose stack on the instance, wire the real public **CORS origin**, verify the
  app end-to-end, decide SQLite vs PostgreSQL-on-instance + EBS snapshot backups.
- **10C:** extend the GitHub Actions workflow to deploy **after** the Phase 9
  gates pass, using **GitHub OIDC → a scoped IAM role** (no long-lived AWS keys
  in repo secrets).
- **10D:** runtime/IAM hardening, address Phase 9 container findings (hardened
  base image) and the documented torch dependency exceptions, logging/monitoring,
  optional nginx reverse proxy to retire the public `8000`, EBS snapshot/DLM.

Nothing in 10B–10D is implemented in this phase.
