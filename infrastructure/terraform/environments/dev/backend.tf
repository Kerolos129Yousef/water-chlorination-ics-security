# Remote state backend (S3) with NATIVE S3 locking (use_lockfile = true).
# NO DynamoDB table is used for locking.
#
# The bucket must already exist (create it via ../../bootstrap) BEFORE running
# `terraform init` here. `bucket` is intentionally left unset so it is supplied
# at init time and no account-specific name is committed:
#
#   terraform init \
#     -backend-config="bucket=ics-guardian-tfstate-<ACCOUNT_ID>" \
#     -backend-config="region=eu-central-1"
#
# For Phase 10A validation only (no bucket yet), initialise WITHOUT a backend:
#   terraform init -backend=false     # providers only; enables `validate`
terraform {
  backend "s3" {
    key          = "ics-guardian/dev/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
    # bucket = supplied via -backend-config at init time.
  }
}
