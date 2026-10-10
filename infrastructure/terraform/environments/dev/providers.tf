provider "aws" {
  region = var.region

  # Tags applied to every taggable resource created by this configuration.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
