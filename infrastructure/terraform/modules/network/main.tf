# ---------------------------------------------------------------------------
# Minimal public network for a single EC2 instance.
#
# Deliberately NOT created (cost + YAGNI for this architecture):
#   * No private subnets  -> the one instance is public-facing by design.
#   * No NAT Gateway       -> ~$32/mo + data charges; the instance reaches the
#                             internet directly through the IGW for Docker Hub
#                             pulls and OS updates.
#   * No ALB               -> the app is a single node; the browser hits the
#                             instance's public IP directly.
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = var.availability_zone

  # The instance needs a routable address; the public IPv4 charge is documented
  # in the cost section. Disable (and use SSM only) to drop that charge.
  map_public_ip_on_launch = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-public-rt" })
}

resource "aws_route" "default_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security group.
#
# Inbound: ONLY TCP 80 (the nginx single web entry point), restricted to
#   `dashboard_ingress_cidrs`. That list is EMPTY by default, so by default NO
#   inbound rule is created at all and the box is reachable only via SSM Session
#   Manager. Port 80 opens only when the owner supplies CIDRs.
#   * NO inbound 8000  -> the FastAPI backend is reached only via the nginx /api
#                         proxy over the private Docker network; never public.
#   * NO inbound SSH (22) -> management is via SSM Session Manager (compute module).
#   * NO inbound 5432     -> PostgreSQL, if used, stays on the Docker network only.
# Outbound: all (Docker Hub image pulls, OS/package updates, SSM endpoints).
# ---------------------------------------------------------------------------
resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-app-sg"
  description = "ICS Guardian app node: inbound web (:80) only; egress all."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-app-sg" })
}

# The ONLY inbound rule: port 80 (the nginx web entry point) for the supplied
# CIDRs. nginx reverse-proxies /api to the backend over the PRIVATE Docker
# network (Phase 10B), so the backend's port 8000 is NEVER exposed in the
# security group. PostgreSQL (5432) is likewise never opened. With the default
# empty `dashboard_ingress_cidrs`, no inbound rule exists at all (SSM-only).
resource "aws_vpc_security_group_ingress_rule" "dashboard_http" {
  for_each          = toset(var.dashboard_ingress_cidrs)
  security_group_id = aws_security_group.app.id
  description       = "Operator dashboard + proxied API (nginx, single web entry point)"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.app.id
  description       = "All egress (Docker Hub pulls, OS updates, SSM)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
