output "alb_dns_name" {
  description = "Public ALB DNS — use as Locust --host"
  value       = "http://${aws_lb.main.dns_name}"
}

output "streaming_ecr_url" {
  value = aws_ecr_repository.streaming.repository_url
}

output "cdn_ecr_url" {
  value = aws_ecr_repository.cdn.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}
