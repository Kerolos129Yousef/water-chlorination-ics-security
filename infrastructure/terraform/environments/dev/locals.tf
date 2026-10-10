locals {
  # Consistent resource-name prefix, e.g. "ics-guardian-dev".
  name_prefix = "${var.project}-${var.environment}"

  # Common tags. Project / Environment / ManagedBy are also applied as provider
  # default_tags; repeated here for modules that build Name tags from them.
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
