# Terraform remote-state bootstrap

This configuration creates the **S3 bucket** that stores the remote Terraform
state for `environments/dev`. It is a separate, one-time bootstrap with its own
**local** state (it cannot keep its state in the bucket it is creating).

> **Phase 10A status:** NOT applied. These files are written and validated only.
> The owner runs the `apply` below when ready to start Phase 10B.

## Why a separate config?

The `environments/dev` configuration uses an S3 backend. A backend bucket must
already exist before `terraform init` can use it — Terraform cannot create its
own backend bucket during init. So we create it here first, with local state.

## State locking

Locking is handled by the S3 backend's native `use_lockfile = true` (a
`<key>.tflock` object written next to the state object). **No DynamoDB table**
is created or required.

## One-time apply (run later, by the owner)

```bash
cd infrastructure/terraform/bootstrap

# Pick a GLOBALLY-UNIQUE bucket name. Suggested: ics-guardian-tfstate-<ACCOUNT_ID>
terraform init                       # local backend; downloads the AWS provider
terraform plan  -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'
terraform apply -var='state_bucket_name=ics-guardian-tfstate-<ACCOUNT_ID>'
```

Record the `state_bucket_name` output; it feeds the `environments/dev` backend.

## Teardown (only when retiring the whole project)

The bucket has `prevent_destroy = true` and versioning. To remove it you must
first empty all object versions, then remove the `prevent_destroy` guard, then
`terraform destroy`. Do this only after every environment has been destroyed.
