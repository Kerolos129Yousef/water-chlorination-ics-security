# ICS Guardian — Terraform (Phase 10A foundation)

Infrastructure-as-Code for deploying the Water Chlorination ICS anomaly-detection
service to AWS. **Phase 10A writes and validates this code only — nothing here
has been applied to AWS.** Deployment happens in Phase 10B.

## Selected architecture (Option A)

A **single EC2 instance running the existing Docker Compose stack** (FastAPI
backend + nginx dashboard, SQLite alert persistence on an encrypted gp3 volume).
No ALB, no RDS, no NAT Gateway — see `../../docs/provenance/phase10a_aws_terraform_foundation.md`
for the full comparison, cost model, and rationale.

* **Region:** `eu-central-1` (Frankfurt) — closest full-service region to the UAE
  with free-tier-eligible **x86** instances. `me-central-1` (UAE) was rejected:
  it has no T-family / free-tier instance and only arm64 Graviton small types
  (higher cost + golden-vector FP risk).
* **Compute:** `t3.small` (x86_64, 2 GiB), AL2023, IMDSv2 required.
* **Access:** SSM Session Manager (no inbound SSH).
* **State:** S3 backend with native `use_lockfile = true` locking (no DynamoDB).

## Directory layout

```
infrastructure/terraform/
├── README.md                 # this file
├── bootstrap/                # one-time: creates the S3 remote-state bucket (local state)
├── modules/
│   ├── network/              # VPC, public subnet, IGW, route table, security group
│   └── compute/              # IAM instance role (SSM-only), EC2 instance, gp3 root
└── environments/
    └── dev/                  # root config: wires the modules, S3 backend, outputs
```

## Phase 10A validation (no AWS changes)

```bash
terraform -chdir=infrastructure/terraform/environments/dev fmt -check -recursive
terraform -chdir=infrastructure/terraform/environments/dev init -backend=false
terraform -chdir=infrastructure/terraform/environments/dev validate
```

## Later: bootstrap → init → plan → apply (Phase 10B, run by the owner)

```bash
# 1) Create the remote-state bucket (once).
cd infrastructure/terraform/bootstrap
terraform init
terraform apply -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'

# 2) Initialise the dev environment against that bucket.
cd ../environments/dev
terraform init \
  -backend-config="bucket=ics-guardian-tfstate-<ACCOUNT_ID>" \
  -backend-config="region=eu-central-1"

# 3) Review, then apply.
terraform plan -out=plan.tfplan     # review the resource plan + cost
terraform apply plan.tfplan
```

## Teardown

```bash
cd infrastructure/terraform/environments/dev
terraform destroy          # removes the VPC, instance, EBS, IAM role, SG
# The remote-state bucket (bootstrap) is retained by design (prevent_destroy).
```
