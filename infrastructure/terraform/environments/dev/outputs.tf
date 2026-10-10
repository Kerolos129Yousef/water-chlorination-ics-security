output "region" {
  description = "Region the environment is deployed into."
  value       = var.region
}

output "vpc_id" {
  description = "VPC ID."
  value       = module.network.vpc_id
}

output "instance_id" {
  description = "EC2 instance ID. Open a shell with: aws ssm start-session --target <id>"
  value       = module.compute.instance_id
}

output "public_ip" {
  description = "Instance public IPv4 (dashboard: http://<ip>/ , API: http://<ip>:8000)."
  value       = module.compute.public_ip
}

output "public_dns" {
  description = "Instance public DNS name."
  value       = module.compute.public_dns
}

output "ssm_session_command" {
  description = "Ready-to-run command for an SSH-less shell via Session Manager."
  value       = "aws ssm start-session --target ${module.compute.instance_id} --region ${var.region}"
}
