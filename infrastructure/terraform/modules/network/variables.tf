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
    CIDRs allowed to reach the single web entry point on TCP 80 (nginx, which
    proxies /api to the backend over the private Docker network). The backend
    port 8000 is NEVER opened here. SAFE BY DEFAULT: empty, so NO public ingress
    rule is created and the box is reachable only via SSM Session Manager. The
    owner must explicitly supply approved CIDRs (e.g. ["203.0.113.4/32"] for
    their own IP). Setting ["0.0.0.0/0"] opens :80 to the whole internet — only
    do that deliberately for a public demo.
  EOT
  type        = list(string)
  default     = []

  validation {
    # Guard-rail: forbid a partial/overly-broad CIDR mix. If 0.0.0.0/0 is used
    # it must be the ONLY entry (makes "open to the world" an explicit choice).
    condition     = !contains(var.dashboard_ingress_cidrs, "0.0.0.0/0") || length(var.dashboard_ingress_cidrs) == 1
    error_message = "If you open to 0.0.0.0/0, it must be the only CIDR in the list (explicit all-internet choice)."
  }
}

variable "tags" {
  description = "Common tags merged onto taggable resources."
  type        = map(string)
  default     = {}
}
