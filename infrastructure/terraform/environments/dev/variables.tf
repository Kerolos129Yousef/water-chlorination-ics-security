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
    AWS region. Default eu-central-1 (Frankfurt): the closest full-service region
    to the UAE that offers free-tier-eligible x86 instance types. The initial
    candidate me-central-1 (UAE) was rejected for Phase 10A because it offers NO
    T-family / free-tier-eligible instance and only arm64 Graviton small types
    (cost + golden-vector FP risk). See PHASE10A docs for the evidence.
  EOT
  type        = string
  default     = "eu-central-1"
}

variable "instance_type" {
  description = "EC2 instance type (x86_64 to preserve golden-vector parity)."
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
  description = "CIDRs allowed to reach the dashboard (80) and API (8000). Restrict to the operator's IP for a locked-down deployment."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "ssm_ami_parameter" {
  description = "SSM public parameter that resolves the latest AL2023 x86_64 AMI. Keeps AMI IDs out of source and always current."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
