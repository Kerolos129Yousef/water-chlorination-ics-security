# ---------------------------------------------------------------------------
# Data sources: resolve the AZ and AMI LIVE so nothing environment-specific is
# hard-coded. The AZ comes from the account's available zones; the AMI from the
# Amazon-maintained AL2023 SSM public parameter.
# ---------------------------------------------------------------------------
data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "al2023_ami" {
  name = var.ssm_ami_parameter
}

module "network" {
  source = "../../modules/network"

  name_prefix             = local.name_prefix
  vpc_cidr                = var.vpc_cidr
  public_subnet_cidr      = var.public_subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  dashboard_ingress_cidrs = var.dashboard_ingress_cidrs
  tags                    = local.common_tags
}

module "compute" {
  source = "../../modules/compute"

  name_prefix         = local.name_prefix
  ami_id              = data.aws_ssm_parameter.al2023_ami.value
  instance_type       = var.instance_type
  subnet_id           = module.network.public_subnet_id
  security_group_id   = module.network.security_group_id
  root_volume_size_gb = var.root_volume_size_gb
  tags                = local.common_tags
}

# CI/CD deploy identity: the IAM role GitHub Actions assumes via OIDC to run the
# SSM-driven deploy. Created regardless of whether CD is wired up yet; it is
# free and inert until the workflow's deploy job assumes it. ssm:SendCommand is
# scoped to this environment's app instance(s) by tag.
module "cicd" {
  source = "../../modules/cicd"

  name_prefix        = local.name_prefix
  github_owner       = var.github_owner
  github_repo        = var.github_repo
  github_environment = var.deploy_environment
  region             = var.region
  instance_tags = {
    Project     = var.project
    Environment = var.environment
  }
  tags = local.common_tags
}
