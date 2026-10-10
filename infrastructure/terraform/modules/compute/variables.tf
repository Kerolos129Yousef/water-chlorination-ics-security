variable "name_prefix" {
  description = "Prefix for resource names/tags (e.g. ics-guardian-dev)."
  type        = string
}

variable "ami_id" {
  description = "AMI ID for the instance. Resolved live from the AL2023 SSM public parameter by the caller (never hard-coded)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. Default t3.small (x86_64, 2 GiB) gives torch headroom and preserves x86 golden-vector parity."
  type        = string
  default     = "t3.small"
}

variable "subnet_id" {
  description = "Public subnet ID to launch into."
  type        = string
}

variable "security_group_id" {
  description = "Security group ID to attach."
  type        = string
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size (GiB). Holds the OS, Docker images, and the SQLite alert volume."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Common tags merged onto taggable resources."
  type        = map(string)
  default     = {}
}
