output "state_bucket_name" {
  description = "Name of the S3 bucket holding remote Terraform state. Feed this into environments/dev backend configuration."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "ARN of the remote-state bucket (used to scope least-privilege state access)."
  value       = aws_s3_bucket.state.arn
}

output "state_bucket_region" {
  description = "Region of the remote-state bucket; must match the backend `region` in environments/dev."
  value       = var.region
}
