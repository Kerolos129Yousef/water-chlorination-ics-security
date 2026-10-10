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
# * user_data installs Docker + the compose plugin, then formats (first boot
#   only) and mounts the dedicated DATA volume at /data with the right ownership
#   for the non-root backend container. It does NOT pull or run the application
#   (that is the SSM-driven deploy step; no registry credentials on the host).
#
# EBS DURABILITY (important, honest statement):
#   ROOT volume: delete_on_termination = true -> holds the OS and Docker images
#     only. Survives stop/start + reboot; deleted on terminate/replace.
#   DATA volume (aws_ebs_volume.data, below): a SEPARATE encrypted gp3 volume
#     mounted at /data that holds the SQLite alert DB. Because it is its own
#     resource (not a block device of the instance), it SURVIVES instance
#     stop/start, reboot, AND terminate/replace (the volume is detached, not
#     deleted). It is destroyed only by `terraform destroy` (or if the volume
#     resource itself is removed). It is AZ-bound: instance + volume share one AZ.
#   NO snapshots/automated backups are configured (cost). Backup/restore is a
#   documented MANUAL procedure (see phase10b_ec2_deployment.md).
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

    # --- Prepare the durable DATA volume at /data (idempotent; NEVER reformats) ---
    DATA_MNT=/data
    mkdir -p "$DATA_MNT"
    # The data device is the attached disk that is neither the root disk nor
    # already mounted. On nitro (t3) EBS shows up as /dev/nvmeXn1, so we detect
    # it by role rather than trusting a fixed device name.
    root_disk="$(findmnt -no SOURCE / | sed -E 's/p?[0-9]+$//')"
    data_dev=""
    for d in $(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}'); do
      [ "$d" = "$root_disk" ] && continue
      if [ -z "$(lsblk -no MOUNTPOINT "$d" | tr -d '[:space:]')" ]; then data_dev="$d"; break; fi
    done
    if [ -n "$data_dev" ]; then
      # Only make a filesystem if the volume is blank -> preserves data on reattach.
      if ! blkid "$data_dev" >/dev/null 2>&1; then mkfs.ext4 -L ics-data "$data_dev"; fi
      uuid="$(blkid -s UUID -o value "$data_dev")"
      grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $DATA_MNT ext4 defaults,nofail 0 2" >> /etc/fstab
      mount -a
    fi
    # The backend image runs as uid/gid 10001 (non-root `app`). The host dir must
    # be writable by that uid for the bind-mounted SQLite DB.
    chown 10001:10001 "$DATA_MNT"
    chmod 0750 "$DATA_MNT"
    # The node is Docker-ready with a mounted /data. App deploy is a separate,
    # SSM-driven step (deploy/deploy.sh) - NO images pulled or run here.
  EOF

  # Re-run user_data only when it actually changes (not on every AMI refresh).
  user_data_replace_on_change = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-app" })
}

# ---------------------------------------------------------------------------
# Dedicated DATA volume for the SQLite alert DB. Separate from the instance's
# root device so it survives instance terminate/replace (detached, not deleted).
# Encrypted gp3. AZ-bound to the instance's AZ.
# ---------------------------------------------------------------------------
resource "aws_ebs_volume" "data" {
  availability_zone = aws_instance.app.availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"
  encrypted         = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-data" })
}

resource "aws_volume_attachment" "data" {
  device_name = var.data_volume_device_name
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id

  # Do NOT force-detach on destroy, and leave the volume intact if only the
  # attachment is removed -> protects the data from accidental loss.
  stop_instance_before_detaching = true
}
