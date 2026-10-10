variable "name_prefix" {
  description = "Prefix for resource Name tags (e.g. ics-guardian-dev)."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. /24 is ample for a single-instance deployment."
  type        = string
  default     = "10.20.0.0/24"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet that hosts the instance."
  type        = string
  default     = "10.20.0.0/28"
}

variable "availability_zone" {
  description = "AZ for the public subnet. Chosen by the caller from a data source so no AZ name is hard-coded."
  type        = string
}

variable "dashboard_ingress_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the dashboard (TCP 80) and the API (TCP 8000).
    Defaults to the whole internet for a public portfolio demo; restrict to the
    operator's IP (e.g. ["203.0.113.4/32"]) for a locked-down deployment.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "api_port" {
  description = "Backend API port exposed for the browser's client-side fetch (FastAPI/uvicorn)."
  type        = number
  default     = 8000
}

variable "tags" {
  description = "Common tags merged onto taggable resources."
  type        = map(string)
  default     = {}
}
