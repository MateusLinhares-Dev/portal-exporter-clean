output "instance_id" {
  value = aws_instance.app.id
}

output "instance_private_ip" {
  value = aws_instance.app.private_ip
}

output "rds_endpoint" {
  value = aws_db_instance.postgres.address
}

output "rds_port" {
  value = aws_db_instance.postgres.port
}

output "database_secret_arn" {
  value = aws_secretsmanager_secret.database.arn
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
