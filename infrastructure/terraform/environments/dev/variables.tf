variable "project" {
  description = "Project slug for naming/tagging."
  type        = string
  default     = "ics-guardian"
}

variable "environment" {
  description = "Environment name (dev/staging/prod). Drives naming and the Environment tag."
  type        = string
  default     = "dev"
}

variable "region" {
  description = <<-EOT
    AWS region. Default eu-central-1 (Frankfurt): a mature full-service region
    with strong connectivity from Egypt that offers x86 T3 instance types (which
    AWS lists as Free-Tier-eligible under the current program; actual account
    eligibility is NOT assumed). me-central-1 (UAE) was rejected: it offers NO
    T-family instance, only arm64 Graviton small types (higher cost +
    golden-vector FP risk). me-south-1 (Bahrain) is a viable alternative (has
    T3/T4g, marginally pricier) but less mature. See the PHASE10A docs for the
    full evidence and comparison.
  EOT
  type        = string
  default     = "eu-central-1"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type (x86_64 to preserve golden-vector parity). Default
    t3.small (2 GiB) gives torch headroom. Free-Tier note: t3.small is listed
    Free-Tier-eligible only for accounts created on/after 2025-07-15; older
    accounts get t2.micro/t3.micro only. t3.micro (1 GiB) is eligible under both
    programs but is RAM-tight for torch. Account eligibility is not assumed.
  EOT
  type        = string
  default     = "t3.small"
}

variable "root_volume_size_gb" {
  description = "Root EBS gp3 size (GiB)."
  type        = number
  default     = 30
}

variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
  default     = "10.20.0.0/24"
}

variable "public_subnet_cidr" {
  description = "Public subnet CIDR."
  type        = string
  default     = "10.20.0.0/28"
}

variable "dashboard_ingress_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the dashboard (80) and API (8000). SAFE BY DEFAULT:
    empty -> no public ingress rule is created; the instance is reachable only
    via SSM Session Manager until the owner supplies approved CIDRs (e.g. their
    own IP as ["203.0.113.4/32"]). Use ["0.0.0.0/0"] only for a deliberate
    public demo.
  EOT
  type        = list(string)
  default     = []
}

variable "ssm_ami_parameter" {
  description = "SSM public parameter that resolves the latest AL2023 x86_64 AMI. Keeps AMI IDs out of source and always current."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
