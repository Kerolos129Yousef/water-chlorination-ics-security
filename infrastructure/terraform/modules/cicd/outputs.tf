output "deploy_role_arn" {
  description = "ARN of the role GitHub Actions assumes via OIDC. Put this in the repo secret AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC provider used (created here or pre-existing)."
  value       = local.oidc_provider_arn
}
