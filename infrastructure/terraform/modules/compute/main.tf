# ---------------------------------------------------------------------------
# IAM instance role — LEAST PRIVILEGE.
#
# The only attached policy is the AWS-managed AmazonSSMManagedInstanceCore,
# which grants exactly what Session Manager needs (no inbound SSH) and nothing
# else. No admin, no s3:*, no ec2:*. Phase 10B/10D may add narrowly-scoped
# policies (e.g. read one Secrets Manager secret for a private Docker Hub pull,
# or CloudWatch logs) — added explicitly, never broadened to a wildcard.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-instance-role"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
  tags               = var.tags
}

# Session Manager access (remote shell without opening port 22).
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance-profile"
  role = aws_iam_role.instance.name
  tags = var.tags
}

# ---------------------------------------------------------------------------
# The instance.
#
# * IMDSv2 required (http_tokens = required) — blocks SSRF-style metadata theft.
# * Encrypted gp3 root volume — encryption at rest, cheaper/faster than gp2.
# * T3 "standard" CPU-credit mode (explicit) — see credit_specification below.
# * user_data installs Docker + the compose plugin and enables the daemon, so
#   the node is READY for Phase 10B. It does NOT pull or run the application
#   (no app deployment in Phase 10A, and no registry credentials on the host).
#
# EBS DURABILITY (important, honest statement):
#   The root volume uses delete_on_termination = true. Therefore:
#     * STOP/START and REBOOT  -> the root volume (and its SQLite data) PERSIST.
#     * TERMINATE / REPLACE    -> the root volume is DELETED with the instance;
#                                 SQLite alert history is LOST.
#   No EBS snapshots or automated backups are configured in Phase 10A. Before
#   Phase 10B deploys the app, the owner must decide the SQLite persistence /
#   backup strategy (e.g. a dedicated encrypted data volume with
#   delete_on_termination = false, and/or a snapshot/DLM schedule). Those are
#   recurring-cost resources and are deliberately NOT added here.
# ---------------------------------------------------------------------------
resource "aws_instance" "app" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [var.security_group_id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  # T3 burstable credits: pin "standard" so the instance can NEVER silently
  # bill surplus CPU-credit charges (the "unlimited" default would). It still
  # bursts using accrued/launch credits; sustained load beyond baseline just
  # throttles rather than costing extra — the right trade-off for a learning
  # /demo box where predictable cost matters more than guaranteed burst.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true
    tags                  = merge(var.tags, { Name = "${var.name_prefix}-root" })
  }

  user_data = <<-EOF
    #!/bin/bash
    set -euo pipefail
    # AL2023: install Docker engine + the compose v2 plugin. SSM agent ships
    # preinstalled, so no inbound SSH is required to manage this host.
    dnf -y update
    dnf -y install docker
    systemctl enable --now docker
    usermod -aG docker ec2-user
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fsSL \
      "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    # Phase 10A ends here: the node is Docker-ready. Phase 10B deploys the app.
  EOF

  # Re-run user_data only when it actually changes (not on every AMI refresh).
  user_data_replace_on_change = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-app" })
}
