# Phase 10B — Deploy ICS Guardian to AWS EC2 via Docker Hub

**Status: PREPARATION ONLY. Nothing is deployed.** No AWS resources were
created, no `terraform apply` was run, and no Docker images were published in
this phase. This document is the reviewed deployment plan + runbook; the owner
approves the Terraform plan and then runs the steps in §9.

- **Branch:** `phase10b-ec2-deployment` (base `main` @ `b30264a`).
- **Architecture:** Internet → nginx (`:80`, single public entry point) → FastAPI
  backend over a private Docker network → SQLite on a dedicated encrypted EBS
  data volume mounted at `/data`.
- **Registry:** Docker Hub `kerolosyousef`, **public** repos (owner-approved).
- **Region / instance:** `eu-central-1`, `t3.small` x86_64, AL2023 (from Phase 10A).
- **Exposure (owner-approved 2026-10-10):** worldwide HTTP on **TCP 80 only**
  (`dashboard_ingress_cidrs = ["0.0.0.0/0"]`); 8000 & 5432 stay closed. Plain
  HTTP, no auth, no TLS → **synthetic, non-sensitive data only** (see §5).

---

## 1. Read-only AWS audit (performed)

| Check | Result |
|---|---|
| Identity | valid IAM user (account id redacted from this report) |
| `ics-guardian` VPCs / instances in eu-central-1 | **0 / 0** (nothing pre-existing) |
| `ics-guardian-tfstate*` S3 buckets | **0** (state bucket not yet created) |
| EC2 On-Demand Standard vCPU quota (eu-central-1) | **16** (t3.small = 2 vCPU → ample) |
| Free Tier | Only "Always Free" offers verified; **this account's EC2/EBS/IPv4 Free-Tier eligibility is NOT verifiable** without billing-console access. Costs below assume **PAID** (see Phase 10A §8). |

No resources were created or modified during the audit.

---

## 2. Docker Hub (verified via public API)

- Account `kerolosyousef` has 8 **public** repos today; **neither
  `ics-guardian-backend` nor `ics-guardian-frontend` exists yet**.
- Docker Hub auto-creates a repo on first push (public for this account).
- **Owner decision (confirmed):** the two new repos will be **public** →
  **the EC2 instance pulls with NO credentials**; no Secrets Manager / IAM
  secret wiring is needed, and no registry secret ever touches the host,
  user-data, Terraform, or git.
- **Image names:** `kerolosyousef/ics-guardian-backend`,
  `kerolosyousef/ics-guardian-frontend`.
- **Tags:** immutable **commit-SHA** (e.g. `:<sha>`). A `:latest` tag may be
  added additionally but the deploy always references the commit SHA.
- **Not done here:** no push. Publishing is an outward action requiring the
  owner's go-ahead and an interactive `docker login` on the owner's machine.

---

## 3. Application + routing (inspected, minimal change made)

- The dashboard (`frontend/index.html`) built API URLs from
  `?api=` **defaulting to `http://127.0.0.1:8000`** — i.e. the *user's own
  browser localhost*, which would NOT reach EC2.
- **Change made (this branch):** the default is now the **same-origin `/api`**
  base; the `?api=` override is preserved for local/direct use. The dashboard
  calls `/api/status`, `/api/alerts/active`, `/api/alerts?limit=50`.
- **nginx (`docker/nginx.conf`)** now reverse-proxies `location /api/` →
  `http://backend:8000` over the private Docker network, **stripping the `/api/`
  prefix** so the backend still sees `/status`, `/health`, `/score`, `/alerts`,
  `/alerts/active` (API semantics unchanged). Uses Docker's embedded DNS
  resolver so a backend restart is re-resolved.
- **CORS:** unchanged and now irrelevant for the dashboard (same-origin via the
  proxy). No CORS rule was broadened. The backend's existing dev allow-list
  stays as-is.
- **No WebSocket/streaming:** the dashboard polls plain JSON GETs; no streaming
  paths to preserve.
- **HTTP only:** the demo uses plain HTTP on port 80. **No TLS/domain** is
  configured (no cert). Documented as acceptable for the demo; HTTPS would need
  a domain + certificate (deferred).

The base `docker-compose.yml` (local dev/packaging contract, asserted by
`tests/test_docker_packaging.py`) is **unchanged**; cloud deploy uses a separate
`deploy/docker-compose.aws.yml`.

---

## 4. Persistence & EBS durability (design + honest behavior)

SQLite lives at `/data/alerts.sqlite3`. `/data` is a **dedicated encrypted gp3
EBS data volume** (`aws_ebs_volume.data`, 8 GiB) — a *separate resource* from the
instance root volume — mounted on the host and bind-mounted into the backend
(`- /data:/data`). The instance user-data formats it **only if blank** (never
reformats on reattach), mounts it by UUID via `/etc/fstab` (`nofail`), and
`chown 10001:10001 /data` so the non-root backend (`uid 10001`) can write it.

| Event | SQLite data outcome |
|---|---|
| `docker compose restart backend` / container recreate | **Preserved** (data is on the host volume, not the container) |
| Instance **reboot** / **stop→start** | **Preserved** |
| Instance **terminate / replace** (incl. Terraform forcing a new instance) | **Preserved** — the data volume is a separate resource; it detaches and reattaches to the new instance; user-data remounts without reformatting |
| **AZ change** | Volume is AZ-bound; a different-AZ instance **cannot attach** it → restore from a snapshot in the new AZ |
| `terraform destroy` | **DELETED** — `destroy` removes `aws_ebs_volume.data` (and its data). Take a final snapshot first if you want to keep it |

**No automated backups/snapshots are configured** (avoids recurring cost).

### Manual backup / restore (demo-appropriate)

Backup (point-in-time, while running — SQLite WAL-safe enough for a demo; for a
clean copy, `docker compose stop backend` first):
```bash
VOL=$(terraform -chdir=infrastructure/terraform/environments/dev output -raw data_volume_id)
aws ec2 create-snapshot --region eu-central-1 --volume-id "$VOL" \
  --description "ics-guardian /data $(date -u +%FT%TZ)" \
  --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Project,Value=ics-guardian}]'
```
Restore: create a new gp3 volume **from the snapshot in the instance's AZ**,
detach the current data volume, attach the restored one, and remount (user-data
logic, or `mount -a`). A full 8 GiB snapshot is ~\$0.40/mo (incrementals less);
**not enabled by default**.

Credentials / DB contents are never committed (SQLite files are git-ignored).

---

## 5. Networking & access

- **Single public port: 80** (nginx). **Port 8000 is NOT in the security group**
  (the Phase 10A `backend_api`/port-8000 ingress rule was **removed** this phase —
  the backend is reachable only via the nginx proxy on the private network).
  **PostgreSQL 5432 is never opened.**
- **Ingress CIDR — OWNER-APPROVED PUBLIC (2026-10-10):** the owner formally
  approved **worldwide HTTP access on TCP 80 only** for multi-person demo
  testing. The Terraform module default stays **`[]`** (fail-safe); the approved
  value is passed **explicitly** at apply:
  `-var 'dashboard_ingress_cidrs=["0.0.0.0/0"]'` (visible/reviewed each run, not a
  committed default). A validation guard rejects mixing `0.0.0.0/0` with other
  CIDRs. To lock down later, set it to a `/32` — no app redeploy needed. The
  approval covers **only** TCP 80; 8000 and 5432 stay closed.
  - **Security implications of public `0.0.0.0/0:80` (accepted):** the dashboard
    and the proxied API become world-reachable over **plain HTTP with NO
    authentication and NO TLS**. `/score` (POST) is reachable via `/api/score`.
    **Operating rule: the demo MUST handle only SYNTHETIC, non-sensitive
    telemetry — never real or confidential data.** Mitigations when wanted:
    restrict the CIDR to known IPs, add authentication, and add TLS (a domain +
    certificate) — all deferred (future work / Phase 10C+).
- **No inbound SSH.** Administration is via **SSM Session Manager**, using the
  Phase 10A instance role (`AmazonSSMManagedInstanceCore` only — least privilege,
  unchanged). Egress is open for Docker Hub pulls, OS updates, and SSM. **No NAT
  Gateway** (the instance is in a public subnet with an IGW route).

---

## 6. Cost (recomputed for the actual plan, current Frankfurt pricing)

Assumes **no Free Tier** (conservative) and 24/7 running:

| Item | Unit | Monthly |
|---|---|---|
| EC2 `t3.small` (Linux, standard credits) | $0.0240/hr | **$17.52** |
| EBS gp3 root, 30 GiB | $0.0952/GB-mo | **$2.86** |
| EBS gp3 **data**, 8 GiB | $0.0952/GB-mo | **$0.76** |
| Public IPv4 (in-use) | $0.005/hr | **$3.65** |
| S3 remote state (tiny, versioned) | usage | **~$0.01** |
| Data transfer out (demo) | 100 GB/mo free, then $0.09/GB | **~$0** |
| **Total (24/7)** | | **≈ $24.8 / month** |

- **Stop the instance between demos** → the public IPv4 charge stops; you keep
  paying only EBS (~$3.6/mo for 38 GiB). Data on both volumes persists.
- Optional manual snapshot of the 8 GiB data volume ≈ **$0.40/mo** (not enabled).
- If the account *does* have Free Tier and a `t3.micro` is used (and the public
  IPv4 is within the 750 h/mo allowance), most of this could be \$0 — **not
  assumed**; confirm in the Billing console.

---

## 7. Blockers / owner actions required before deploy

1. **Approve the Terraform plan** (bootstrap + main) and the **public `0.0.0.0/0:80`** exposure.
2. **Publish images** (not done here): on the owner's machine,
   `docker login` (interactive) then push the commit-SHA-tagged images to the
   two public repos (§9 step 0). First push auto-creates the public repos.
3. **Pick a globally-unique state bucket name** (e.g. `ics-guardian-tfstate-<ACCOUNT_ID>`).

---

## 8. Terraform plan summary (validated; NOT applied)

- `terraform fmt -check` clean; `terraform validate` OK (dev + bootstrap).
- Plan (local-backend, read-only):
  - **Default (`dashboard_ingress_cidrs=[]`): 14 add / 0 change / 0 destroy.**
  - **Approved public (`["0.0.0.0/0"]`): 15 add** — the one extra resource is a
    single **port-80** ingress rule. **No port-8000 rule is ever planned.**
- New vs Phase 10A: `aws_ebs_volume.data` + `aws_volume_attachment.data`
  (the dedicated /data volume); the port-8000 ingress rule was removed.

---

## 9. Exact deployment commands (run after owner approval)

```bash
# 0) PUBLISH IMAGES (owner's machine; interactive login, no tokens in shell/git)
SHA=$(git rev-parse HEAD)
docker login                           # interactive; never paste tokens in chat
docker build --platform linux/amd64 -f docker/backend.Dockerfile  -t kerolosyousef/ics-guardian-backend:$SHA  .
docker build --platform linux/amd64 -f docker/frontend.Dockerfile -t kerolosyousef/ics-guardian-frontend:$SHA .
docker push kerolosyousef/ics-guardian-backend:$SHA
docker push kerolosyousef/ics-guardian-frontend:$SHA
# verify they are PUBLIC:
curl -s https://hub.docker.com/v2/repositories/kerolosyousef/ics-guardian-backend/  | grep -o '"is_private":[a-z]*'
curl -s https://hub.docker.com/v2/repositories/kerolosyousef/ics-guardian-frontend/ | grep -o '"is_private":[a-z]*'

# 1) BOOTSTRAP the remote-state bucket (once)
cd infrastructure/terraform/bootstrap
terraform init
terraform plan  -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'   # REVIEW
terraform apply -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'   # only if approved

# 2) INIT main config against that bucket
cd ../environments/dev
terraform init \
  -backend-config="bucket=ics-guardian-tfstate-<ACCOUNT_ID>" \
  -backend-config="region=eu-central-1"

# 3) PLAN the infrastructure (public demo exposure, explicit)
terraform plan -out=plan.tfplan -var='dashboard_ingress_cidrs=["0.0.0.0/0"]'  # REVIEW

# 4) APPLY (only after review/approval)
terraform apply plan.tfplan
IID=$(terraform output -raw instance_id)
IP=$(terraform output -raw public_ip)

# 5) DEPLOY the app over SSM (no SSH, no secrets). Images are public.
aws ssm send-command --region eu-central-1 \
  --document-name "AWS-RunShellScript" \
  --targets "Key=InstanceIds,Values=$IID" \
  --parameters commands='sudo ICS_IMAGE_TAG='"$SHA"' ICS_RAW_BASE=https://raw.githubusercontent.com/Kerolos129Yousef/water-chlorination-ics-security/'"$SHA"' bash -c "curl -fsSL \$ICS_RAW_BASE/deploy/deploy.sh | bash"'
# (or: open a shell with `aws ssm start-session --target $IID` and run deploy/deploy.sh)

# 6) VERIFY end to end
curl -fsS "http://$IP/healthz"            # nginx up
curl -fsS "http://$IP/api/health"         # backend via proxy: detector_loaded=true
curl -fsS "http://$IP/api/status"         # monitoring rollup
python scripts/run_e2e_demo.py --mode attack --base-url "http://$IP/api"   # score/alerts over the proxy
# open http://$IP/ in a browser → dashboard shows live status
```

### Rollback
Re-run step 5 with `ICS_IMAGE_TAG`/`ICS_RAW_BASE` set to a **previous commit SHA**
(images are immutable per SHA, so rollback is exact). SQLite data is untouched.

### Teardown
```bash
cd infrastructure/terraform/environments/dev
# optional: snapshot the data volume first (see §4)
terraform destroy -var='dashboard_ingress_cidrs=["0.0.0.0/0"]'
# NOTE: this DELETES the data volume and its SQLite data. The state bucket
# (bootstrap) is retained (prevent_destroy).
```

---

## 10. What is deliberately NOT done in this phase

No `terraform apply`, no AWS resource creation, no image push, no ECR, no TLS,
no automated snapshots, no NAT Gateway, no Secrets Manager (public repos),
branch not merged. Phase 10B completes only after the owner approves the plan,
the deploy is performed, and the app is verified end-to-end on the instance.
