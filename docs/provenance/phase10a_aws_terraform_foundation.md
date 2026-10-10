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
| Persistence / recovery | SQLite on the gp3 root volume; app is otherwise stateless (redeploy from image). **No snapshots/backups configured in 10A** — see §11 | managed RDS backups | managed RDS backups |
| Portfolio value | strong: real AWS + Terraform + Docker + SSM + IAM + remote state | strongest but overkill & costly | medium |
| Fits "cost-conscious graduation demo" | **best** | no (recurring ALB+NAT+RDS even idle) | partial |

**Decision — Option A.** It meets the actual graduation-project need (a real,
secure, reproducible single-node cloud deployment of the existing app), gives
genuine AWS/Terraform/Docker/DevSecOps experience, and avoids the always-on ALB
+ NAT + RDS charges that Option B incurs **even with zero traffic**. ECS/Fargate,
ALB, and RDS were explicitly **not** chosen merely for looking production-grade.

---

## 3. Region decision — `eu-central-1` (reassessed vs `me-central-1` and `me-south-1`)

Three Middle-East-relevant candidates were evaluated with read-only AWS queries
(account alias `kyz-v2`; all three regions enabled/opted-in for the account).

**`me-central-1` (UAE) — rejected.** `describe-instance-type-offerings` returns
**no T-family** (`t2/t3/t3a/t4g`) types at all; the smallest general-purpose
types are **arm64 Graviton** `m6g.medium`/`m7g.medium` (4 GiB) at **$0.0473 /
$0.0500 per hr** (~$34–37/mo). Two problems: (a) arm64 is a **golden-vector
risk** — the golden tests are already FP-sensitive across x86 vendors
(`atol=1e-9`), so an arch change is an unnecessary correctness hazard for
immutable ML artifacts; (b) higher cost with no cheap x86 option.

**`me-south-1` (Bahrain) — viable alternative, not selected.** AWS's published
EC2 regional table lists **T3/T4g** in Bahrain, and the Pricing API (queried via
the global `us-east-1` endpoint) confirms real prices: `t3.small` **$0.0251/hr**,
`t3.micro` $0.0125/hr, `t4g.small` $0.0201/hr, gp3 **$0.0968/GB-mo**.
*Verification limitation:* direct `me-south-1` region-endpoint calls
(`describe-availability-zones`, `describe-instance-type-offerings`, SSM AMI
lookup) **timed out / were unreachable from this working environment** on
repeated attempts, so per-AZ offering and the AL2023 AMI could not be confirmed
directly here. They should be re-verified from a working network before
selecting Bahrain.

**`eu-central-1` (Frankfurt) — selected.** Directly verified: 3 AZs, AL2023
x86_64 AMI resolves live via SSM, `t3.small` offered in all AZs. Pricing:
`t3.small` **$0.0240/hr**, `t3.micro` $0.0120/hr, gp3 **$0.0952/GB-mo**.

### Frankfurt vs Bahrain — why Frankfurt stays the default

| Factor | Frankfurt (`eu-central-1`) | Bahrain (`me-south-1`) |
|---|---|---|
| `t3.small` on-demand | **$0.0240/hr** | $0.0251/hr (~5% more) |
| gp3 storage | **$0.0952/GB-mo** | $0.0968/GB-mo (~2% more) |
| x86 T3 available | **yes (verified directly)** | yes (price-confirmed; endpoint probe failed here) |
| Region maturity / service breadth | **very high** (matters for 10C/10D) | smaller/newer region |
| Reachability from this tooling env | **reliable** | endpoint calls timed out |
| Latency from **Egypt** (demo audience) | excellent (major EU peering; ~60–80 ms) | geographically closer but comparable for a demo |
| Golden-vector safety (x86) | **yes** | yes |

Bahrain's only real edge is slight geographic proximity to Egypt, which is not
decisive for an interactive demo (both are well under 100 ms). Frankfurt is
marginally cheaper, far more mature (full service coverage for later phases),
and was fully verifiable from the working environment. **Region remains a
Terraform variable (`var.region`)**; switching to `me-south-1` is a one-line
change if GCC/UAE proximity later becomes a hard requirement — re-verify its AZs
and AMI from a working network first.

> If UAE **data-residency** specifically is later required, the trade-off is
> ~$36+/mo on arm64 Graviton in `me-central-1` **plus** re-verifying the golden
> vectors on Graviton before trusting the model.

---

## 4. Selected infrastructure (what the Terraform creates)

Region `eu-central-1`, AZ chosen from a data source (not hard-coded):

- **Networking** (`modules/network`): one VPC (`10.20.0.0/24`), one **public**
  subnet (`/28`), Internet Gateway, public route table + default route. **No**
  private subnets, **no NAT Gateway**, **no ALB** — not needed for one node.
- **Security group**: inbound **80** (dashboard) and **8000** (API) only, each
  restricted to `var.dashboard_ingress_cidrs`. **SAFE BY DEFAULT: that list is
  EMPTY (`[]`)**, so **no public ingress rule is created** until the owner
  explicitly supplies approved CIDRs — the instance is then reachable only via
  SSM. A validation rule forbids mixing `0.0.0.0/0` with other CIDRs so
  "open to the world" is always a deliberate, standalone choice. **No inbound
  22. No inbound 5432.** Egress all (Docker Hub pulls, OS updates, SSM).
- **Compute** (`modules/compute`): `t3.small` (x86_64, 2 GiB), AL2023 AMI
  resolved live from the SSM public parameter, **IMDSv2 required**, T3 CPU
  credits pinned to **`standard`** (never bills surplus-credit charges; see §12),
  **encrypted gp3** 30 GiB root. `user_data` installs Docker + compose plugin and
  enables the daemon — it makes the node **ready**; it does **not** pull or run
  the app.
- **IAM**: an instance role whose **only** policy is the AWS-managed
  `AmazonSSMManagedInstanceCore` (least privilege; enables Session Manager, no
  SSH). **No admin, no wildcards.**
- **Persistence (honest statement):** SQLite lives on the gp3 **root** volume
  (`delete_on_termination = true`). Stop/start and reboot preserve it;
  **terminating or replacing the instance deletes it**, and **no snapshots or
  backups are configured in 10A**. The persistence/backup decision is required
  before 10B deploys the app — see §11.

Resource plan: **14 to add, 0 to change, 0 to destroy** (§7). With the default
empty ingress list the plan creates **12** resources (the 2 SG ingress rules are
omitted until CIDRs are supplied).

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

With the **safe default** (`dashboard_ingress_cidrs = []`) neither `:80` nor
`:8000` is open — the flows above exist only after the owner supplies CIDRs.

Hardening already encoded: IMDSv2 required, encrypted EBS, SSM instead of SSH,
least-privilege instance role, DB port never in the security group, no public
ingress until explicitly requested.

**Recommended for Phase 10B/10D — single web entry point.** Putting the API
behind the nginx frontend (one port 80/443, `location /api/` → `backend:8000`)
would let us stop exposing `8000` publicly. **This is NOT done here and must not
be done blindly:** the dashboard currently calls the API **directly from the
browser**, and `backend/app.py` CORS only allow-lists `localhost:5500/8080`.
Before consolidating, 10B must (a) point the dashboard's fetch base at the
same-origin `/api` path and (b) set the real public origin in CORS (or make the
calls same-origin so CORS is moot). Application behavior is unchanged in 10A.

---

## 7. Validation performed (no AWS changes)

| Check | Result |
|---|---|
| `terraform fmt -recursive` | clean (no diffs) |
| `terraform validate` (dev) | **Success! The configuration is valid.** |
| `terraform validate` (bootstrap) | **Success! The configuration is valid.** |
| `terraform plan` — **default (empty ingress)** | **Plan: 12 to add, 0 to change, 0 to destroy** |
| `terraform plan` — with one `/32` ingress CIDR | **Plan: 14 to add** (adds the 2 SG rules) |
| `terraform plan` — `["0.0.0.0/0","/32"]` (guard test) | **Rejected**: "If you open to 0.0.0.0/0, it must be the only CIDR…" |
| `cpu_credits` in plan | `"standard"` (no surplus-credit billing) |
| AWS provider resolved | `hashicorp/aws v6.68.0` (locked in `.terraform.lock.hcl`) |
| Ruff gate (`E9,F`) | All checks passed |
| Bandit gate (sev≥medium, conf≥high) | no findings (exit 0) |
| Test suite (`pytest -q`) | **411 passed, 1 pre-existing warning** (unchanged baseline) |
| Protected ML artifacts / golden fixtures | **unchanged** |

The `plan`s were produced with a **temporary local backend override** so no S3
state bucket was created; the override and its local state were removed
afterwards. The **12** default resources: VPC, IGW, subnet, route table, route,
route-table association, security group, egress rule, IAM role, role-policy
attachment, instance profile, EC2 instance. Supplying `dashboard_ingress_cidrs`
adds the 2 ingress rules (→ 14).

---

## 8. Cost model & Free-Tier reality

### How AWS Free Tier actually works (corrected)

Per current AWS documentation
([EC2 Free Tier](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-free-tier-usage.html),
[Billing Free Tier](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier.html)),
benefits depend on **when the account was created** and **which program** it is on:

| | Account created **before 2025-07-15** | Account created **on/after 2025-07-15** |
|---|---|---|
| Free-Tier-eligible instance types | `t2.micro`, `t3.micro` | `t3.micro`, **`t3.small`**, `t4g.micro`, `t4g.small`, `c7i-flex.large`, `m7i-flex.large` |
| EBS eligible | `gp2`/`gp3`/`standard`/`st1`/`sc1` | same |
| Duration / model | **12 months**, usage-limited (overage billed pay-as-you-go) | **6 months** or until **$100 (+up to $100)** credits exhausted |

**Public IPv4 is NOT "never free."** AWS documents a Free-Tier allowance of
**750 hours/month of in-use public IPv4 address at no charge** for eligible
Free-Tier accounts (introduced with the Feb 2024 IPv4 charge); outside that
allowance the standard rate is **$0.005/hr** (≈ $3.65/mo for one always-on
address). An earlier draft of this doc incorrectly stated the IPv4 charge is
never waived — that was wrong.

### This account — what is and isn't verified

- **Verified (read-only):** the account has **"Always Free"** offers active
  (SNS/SQS/Glue/KMS); `describe-instance-types --filters free-tier-eligible`
  returns the **current-program** catalog list (`t3.micro/small`, `t4g.*`,
  `c7i-flex.large`, `m7i-flex.large`).
- **NOT verified:** the account's **creation date**, **program** (12-month vs
  the new credit plan), **remaining credits**, and therefore whether *this*
  account is currently Free-Tier-eligible for EC2/EBS/IPv4. The Free Tier usage
  API showed no "12 Months Free" records, which is **consistent with either** a
  post-July-2025 credit-plan account **or** an account past its 12-month window
  — it does not distinguish them. Confirm in the **Billing console**
  (EC2 dashboard "Free Tier" box / Billing → Free Tier).

**Eligibility note on the default type:** `t3.small` is Free-Tier-eligible
**only** under the new (post-2025-07-15) program. If this account is on the
legacy 12-month tier, only `t2.micro`/`t3.micro` are eligible — switch
`instance_type` to `t3.micro` to stay within free limits (note: 1 GiB RAM is
tight for torch; add swap or accept the ~$17.5/mo paid `t3.small`).

### Conservative PAID estimate (assume no Free Tier applies)

The figures below **assume zero Free-Tier benefit** — a deliberately
conservative upper bound. If the account *is* eligible, some/all of the EC2,
EBS, and the first 750 h of IPv4 could be covered, reducing this materially;
those savings are **not guaranteed** and are not assumed here.

| Item | Unit price (Frankfurt, verified) | Monthly (730 h) |
|---|---|---|
| EC2 `t3.small` (on-demand, Linux, standard credits) | $0.0240/hr | **$17.52** |
| EBS gp3 root, 30 GiB | $0.0952/GB-mo | **$2.86** |
| Public IPv4 address (in-use, if not Free-Tier-covered) | $0.005/hr | **$3.65** |
| S3 remote state (tiny, versioned) | ~usage | **~$0.01** |
| Data transfer out (demo volume) | first 100 GB/mo free, then $0.09/GB | **~$0** |
| **Total (always-on, paid assumption)** | | **≈ $24 / month** |

Cost-control notes:

- **Billed continuously while running (paid assumption):** EC2 compute + the
  public IPv4 address.
- **Billed even when stopped:** the **EBS volume** (~$2.86/mo). Stopping the
  instance releases the auto-assigned public IPv4 (no IPv4 charge while stopped)
  → **stop between demos** to drop to ~$3/mo.
- **T3 credits:** `cpu_credits = "standard"` is set explicitly, so there is **no
  surplus/"unlimited" credit charge** (see §12).
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
  compose stack on the instance, **wire the single web entry point / CORS origin**
  (see §6), **make the SQLite persistence + backup decision** (see §11), and
  verify the app end-to-end.
- **10C:** extend the GitHub Actions workflow to deploy **after** the Phase 9
  gates pass, using **GitHub OIDC → a scoped IAM role** (no long-lived AWS keys
  in repo secrets).
- **10D:** runtime/IAM hardening, address Phase 9 container findings (hardened
  base image) and the documented torch dependency exceptions, logging/monitoring,
  nginx reverse proxy to retire the public `8000`, EBS snapshot/DLM.

Nothing in 10B–10D is implemented in this phase.

---

## 11. SQLite persistence & EBS durability — decision required before 10B

Phase 10A uses an **encrypted gp3 root volume with `delete_on_termination =
true`**. Honest behavior:

- **Reboot** and **stop/start** → the root volume and its SQLite alert DB
  **persist**.
- **Terminate / replace** the instance (incl. a Terraform change that forces
  replacement) → the root volume is **deleted**, and **SQLite alert history is
  lost**.
- **No EBS snapshots or automated backups are configured in Phase 10A.** There
  is no snapshot-based recovery available yet.

The app itself is otherwise **stateless** (immutable model + image), so only the
alert history is at risk. Before 10B deploys the application, the owner must
pick a persistence/backup approach, e.g.:

1. **Dedicated encrypted data volume** for `/data` with
   `delete_on_termination = false` (survives instance replacement), or
2. a **snapshot/DLM schedule** on the volume, or
3. **PostgreSQL** (the existing overlay) on the instance with its own durable
   volume, or
4. explicitly accept that alert history is **ephemeral** for the demo.

Each of 1–3 adds recurring cost, so **none is added in this correction commit**
— the choice is deferred to 10B by design.

---

## 12. T3 CPU-credit mode

The instance pins `credit_specification { cpu_credits = "standard" }`. T3 is a
**burstable** family: in the default **`unlimited`** mode, sustained CPU above
the baseline silently bills **surplus credits** — an easy way to get surprise
charges on a demo box. **`standard`** mode bursts only on accrued/launch credits
and then **throttles to baseline instead of charging extra**, giving predictable
cost. Trade-off: a heavy sustained workload would be throttled rather than
guaranteed full burst — acceptable for a learning/portfolio deployment where
cost predictability matters more. Switch back to `unlimited` deliberately if a
real latency SLA ever requires guaranteed burst.

---

## 13. Merge-time container-scan remediation (urllib3)

While merging this branch, the Phase 9 Trivy gate (Gate 6) flagged two **fixable
HIGH** CVEs in `urllib3 2.7.0` (`CVE-2026-97687` HTTPS-proxy TLS interception;
`CVE-2026-97689` DoS) — newly published since Phase 9 last ran, unrelated to the
Phase 10A diff. Remediation (both verified by inspecting the built image):

1. **Pinned the importable copy** to the patched release:
   `urllib3==2.8.0` in `docker/requirements-runtime.txt`. Trivy then reports **0**
   findings for `/opt/venv/.../urllib3-2.8.0.dist-info`. Not on the ML inference
   path → golden vectors unaffected; `pip-audit` (Gate 4) stays clean.
2. **Documented the remaining copy as a non-reachable exception.** The only
   remaining 2.7.0 is **pip's vendored** `pip/_vendor/urllib3` (confirmed via its
   `_version.py`/`vendor.txt`). pip is **never executed** in the immutable,
   non-root, read-only runtime container (`CMD` is `uvicorn` only), and both CVEs
   require urllib3 to make HTTP requests, which happens only at `pip install`
   time. Added `CVE-2026-97687` + `CVE-2026-97689` to `.trivyignore` with that
   justification — the same class as the 4 pre-existing pip/setuptools-vendored
   entries (so 6 total). The documented Phase 10 follow-up (slim/distroless base
   with no pip in the runtime layer) removes this vendored copy and lets all such
   entries be deleted. No gate policy was weakened.
