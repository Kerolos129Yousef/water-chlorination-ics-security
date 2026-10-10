# Bootstrap: provisions the S3 bucket that later holds the REMOTE Terraform
# state for environments/dev. This is a deliberately separate, self-contained
# configuration with its OWN LOCAL state (there is no remote backend here):
# the bucket cannot store the state of the very run that creates it.
#
# Lifecycle: run `terraform apply` here ONCE (manually, by the owner) to create
# the state bucket, BEFORE initialising environments/dev with its S3 backend.
# It is intentionally NOT applied during Phase 10A.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # No `backend` block on purpose -> local state (terraform.tfstate in this dir).
  # That local state file is git-ignored; keep it safe, it tracks the bucket.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Component = "tf-remote-state"
      ManagedBy = "terraform"
    }
  }
}
