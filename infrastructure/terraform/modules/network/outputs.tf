output "vpc_id" {
  description = "ID of the created VPC."
  value       = aws_vpc.this.id
}

output "public_subnet_id" {
  description = "ID of the public subnet hosting the instance."
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "ID of the app security group to attach to the instance."
  value       = aws_security_group.app.id
}
