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

variable "data_volume_size_gb" {
  description = "Dedicated encrypted gp3 DATA volume size (GiB), mounted at /data for the SQLite alert DB. 8 GiB is ample for alert history."
  type        = number
  default     = 8
}

variable "data_volume_device_name" {
  description = "Block-device name requested for the data volume attachment. On nitro (t3) the kernel remaps this to /dev/nvmeXn1; user-data detects the device by role, not this name."
  type        = string
  default     = "/dev/sdf"
}

variable "tags" {
  description = "Common tags merged onto taggable resources."
  type        = map(string)
  default     = {}
}
