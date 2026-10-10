variable "project" {
  description = "Project slug used for naming and tagging."
  type        = string
  default     = "ics-guardian"
}

variable "region" {
  description = "AWS region for the remote-state bucket. Keep it in the same region as the workloads to avoid cross-region state latency."
  type        = string
  default     = "eu-central-1"
}

variable "state_bucket_name" {
  description = <<-EOT
    Globally-unique S3 bucket name for remote Terraform state. S3 bucket names
    are a GLOBAL namespace, so this must be unique across all of AWS. Suggested
    convention: "<project>-tfstate-<account_id>" (account id keeps it unique).
    Must be set explicitly; there is no safe default for a global name.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name (3-63 chars, lowercase)."
  }
}
