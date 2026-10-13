# ---------------------------------------------------------------------------
# CI/CD deploy identity — GitHub Actions -> AWS via OIDC, LEAST PRIVILEGE.
#
# GitHub Actions presents a short-lived OpenID Connect token; AWS STS exchanges
# it for temporary credentials by assuming the role below. NO long-lived AWS
# access keys are ever stored in GitHub. The trust is pinned to exactly one
# subject: this repo running in the named protected Environment, so only an
# approved deploy job can assume it. The attached policy grants only enough to
# drive the existing SSM deploy (find the instance + RunShellScript on it +
# read the result) and is tag-scoped to the app instance(s).
# ---------------------------------------------------------------------------
data "aws_caller_identity" "current" {}

locals {
  oidc_provider_arn = (
    var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  )
}

# Account-level OIDC provider for GitHub Actions. One per account; see
# create_oidc_provider to reuse an existing one. AWS validates the token signature
# against its own trust store, but the resource still requires a thumbprint; the
# two values below are GitHub's published intermediate-CA thumbprints.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
  tags = var.tags
}

# Trust policy: only `repo:<owner>/<repo>:environment:<env>` may assume, and only
# with the sts.amazonaws.com audience. Pinning to the Environment (not a branch)
# means the protected-Environment approval gate guards credential issuance.
data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_owner}/${var.github_repo}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name_prefix}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
  tags                 = var.tags
}

# Least-privilege deploy policy: enough to locate the instance by tag, run the
# AWS-managed RunShellScript document on it (tag-scoped), and read the result.
data "aws_iam_policy_document" "deploy" {
  # DescribeInstances has no resource-level scoping in IAM; read-only, used to
  # resolve the running instance id from its tags.
  statement {
    sid       = "DescribeInstances"
    effect    = "Allow"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  # Permit sending the AWS-managed shell document...
  statement {
    sid       = "SendCommandDocument"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  # ...but only to instances carrying ALL of the configured tags.
  statement {
    sid       = "SendCommandInstances"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]

    dynamic "condition" {
      for_each = var.instance_tags
      content {
        test     = "StringEquals"
        variable = "aws:ResourceTag/${condition.key}"
        values   = [condition.value]
      }
    }
  }

  # Read command status + stdout/stderr so the job can wait and verify.
  statement {
    sid    = "ReadInvocations"
    effect = "Allow"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.name_prefix}-github-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
