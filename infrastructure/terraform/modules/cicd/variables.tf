variable "name_prefix" {
  description = "Resource-name prefix, e.g. \"ics-guardian-dev\"."
  type        = string
}

variable "github_owner" {
  description = "GitHub org/user that owns the repo (OIDC subject)."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (OIDC subject)."
  type        = string
}

variable "github_environment" {
  description = <<-EOT
    GitHub Actions Environment whose OIDC token may assume the deploy role. The
    trust policy is pinned to `environment:<this>` (NOT a branch), so only a job
    that runs in this protected Environment - i.e. after its required-reviewer
    approval gate - can obtain AWS credentials. Must match the `environment:`
    in the workflow's deploy job.
  EOT
  type        = string
}

variable "region" {
  description = "AWS region (scopes the SSM/EC2 resource ARNs in the deploy policy)."
  type        = string
}

variable "instance_tags" {
  description = <<-EOT
    Tag key/values that scope `ssm:SendCommand` to the application instance(s).
    The role may only run commands on instances carrying ALL of these tags, so a
    leaked token cannot drive arbitrary hosts in the account.
  EOT
  type        = map(string)
}

variable "create_oidc_provider" {
  description = <<-EOT
    Create the account-level GitHub Actions OIDC provider. There can be only one
    per AWS account; set false and the role will reference the existing provider
    if your account already has it (e.g. created by another stack).
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to the IAM resources."
  type        = map(string)
  default     = {}
}
