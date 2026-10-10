output "instance_id" {
  description = "EC2 instance ID (use with SSM: aws ssm start-session --target <id>)."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Public IPv4 of the instance (billed while running; see cost notes)."
  value       = aws_instance.app.public_ip
}

output "public_dns" {
  description = "Public DNS name of the instance."
  value       = aws_instance.app.public_dns
}

output "instance_role_arn" {
  description = "ARN of the instance IAM role (SSM-only least privilege)."
  value       = aws_iam_role.instance.arn
}

output "data_volume_id" {
  description = "ID of the dedicated encrypted EBS data volume (holds the SQLite alert DB at /data). Snapshot this for backups."
  value       = aws_ebs_volume.data.id
}
