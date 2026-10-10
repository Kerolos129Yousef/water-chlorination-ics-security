# ---------------------------------------------------------------------------
# Remote Terraform state bucket.
#
# Hardening applied (all required by the project's cost/security policy):
#   * Versioning           -> state history + recovery from bad applies.
#   * SSE (AES256)          -> encryption at rest, no KMS key cost.
#   * Public access block   -> all four switches ON (never public).
#   * Ownership controls    -> bucket-owner-enforced (ACLs disabled).
#   * TLS-only bucket policy -> deny any non-HTTPS request.
#   * Lifecycle: expire old noncurrent versions so state history stays cheap.
#
# State LOCKING: handled natively by the S3 backend's `use_lockfile = true`
# (a <key>.tflock object in THIS bucket). No DynamoDB table is created.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  # Guard rail: refuse `terraform destroy` from accidentally deleting the
  # bucket that holds every environment's state. Flip deliberately to tear down.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-noncurrent-state"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Deny any request that is not over TLS. Depends on the public-access block so
# the policy is never evaluated while the bucket could still be made public.
resource "aws_s3_bucket_policy" "state_tls_only" {
  bucket     = aws_s3_bucket.state.id
  depends_on = [aws_s3_bucket_public_access_block.state]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.state.arn,
          "${aws_s3_bucket.state.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })
}
