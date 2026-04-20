terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ─── Networking (default VPC) ─────────────────────────────────────────────────

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ─── ECR Repositories ─────────────────────────────────────────────────────────

resource "aws_ecr_repository" "streaming" {
  name                 = "${var.project}-streaming"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_repository" "cdn" {
  name                 = "${var.project}-cdn"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# ─── Image URIs ───────────────────────────────────────────────────────────────

locals {
  streaming_image = "${aws_ecr_repository.streaming.repository_url}:latest"
  cdn_image       = "${aws_ecr_repository.cdn.repository_url}:latest"
}

# ─── Docker build + push ──────────────────────────────────────────────────────

resource "null_resource" "push_streaming" {
  triggers = {
    src        = filemd5("${path.module}/../streaming-service/main.go")
    dockerfile = filemd5("${path.module}/../streaming-service/Dockerfile")
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${aws_ecr_repository.streaming.registry_id}.dkr.ecr.${var.aws_region}.amazonaws.com
      docker build --platform linux/amd64 -t ${local.streaming_image} ${path.module}/../streaming-service
      docker push ${local.streaming_image}
    EOT
  }

  depends_on = [aws_ecr_repository.streaming]
}

resource "null_resource" "push_cdn" {
  triggers = {
    src        = filemd5("${path.module}/../cdn-cache-service/main.go")
    dockerfile = filemd5("${path.module}/../cdn-cache-service/Dockerfile")
  }

  provisioner "local-exec" {
    command = <<-EOT
      docker build --platform linux/amd64 -t ${local.cdn_image} ${path.module}/../cdn-cache-service
      docker push ${local.cdn_image}
    EOT
  }

  depends_on = [aws_ecr_repository.cdn, null_resource.push_streaming]
}

# ─── ECS Cluster ──────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-cluster"
}

# ─── IAM — use pre-existing LabRole (AWS Academy does not allow CreateRole) ───

data "aws_iam_role" "ecs_task_execution" {
  name = "LabRole"
}

# ─── CloudWatch Log Groups ────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "streaming" {
  name              = "/ecs/${var.project}-streaming"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "cdn" {
  name              = "/ecs/${var.project}-cdn"
  retention_in_days = 7
}

# ─── Security Groups ──────────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name   = "${var.project}-alb-sg"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs" {
  name   = "${var.project}-ecs-sg"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port       = 8080
    to_port         = 8081
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port = 8081
    to_port   = 8081
    protocol  = "tcp"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── Application Load Balancer ────────────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "streaming" {
  name        = "${var.project}-streaming-tg"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"

  health_check {
    path                = "/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
    enabled         = true
  }
}

resource "aws_lb_target_group" "cdn" {
  name        = "${var.project}-cdn-tg"
  port        = 8081
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"

  health_check {
    path                = "/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.streaming.arn
  }
}

resource "aws_lb_listener_rule" "cdn" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern { values = ["/cache/*", "/admin/*"] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.cdn.arn
  }
}

# ─── ECS Task Definitions ─────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "streaming" {
  family                   = "${var.project}-streaming"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.streaming_cpu
  memory                   = var.streaming_memory
  execution_role_arn       = data.aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([{
    name      = "streaming-service"
    image     = local.streaming_image
    essential = true
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    environment = [
      { name = "PORT",    value = "8080" },
      { name = "CDN_URL", value = "http://${aws_lb.main.dns_name}" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.streaming.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "streaming"
      }
    }
  }])

  depends_on = [null_resource.push_streaming]
}

resource "aws_ecs_task_definition" "cdn" {
  family                   = "${var.project}-cdn"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cdn_cpu
  memory                   = var.cdn_memory
  execution_role_arn       = data.aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([{
    name      = "cdn-cache-service"
    image     = local.cdn_image
    essential = true
    portMappings = [{ containerPort = 8081, hostPort = 8081, protocol = "tcp" }]
    environment = [{ name = "PORT", value = "8081" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.cdn.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "cdn"
      }
    }
  }])

  depends_on = [null_resource.push_cdn]
}

# ─── ECS Services ─────────────────────────────────────────────────────────────

resource "aws_ecs_service" "streaming" {
  name            = "${var.project}-streaming-svc"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.streaming.arn
  desired_count   = var.streaming_min_capacity
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.streaming.arn
    container_name   = "streaming-service"
    container_port   = 8080
  }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "cdn" {
  name            = "${var.project}-cdn-svc"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.cdn.arn
  desired_count   = var.cdn_min_capacity
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.cdn.arn
    container_name   = "cdn-cache-service"
    container_port   = 8081
  }
  depends_on = [aws_lb_listener.http]
}

# ─── Auto Scaling (Streaming Service) ────────────────────────────────────────

resource "aws_appautoscaling_target" "streaming" {
  max_capacity       = var.streaming_max_capacity
  min_capacity       = var.streaming_min_capacity
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.streaming.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "streaming_cpu" {
  name               = "${var.project}-streaming-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.streaming.resource_id
  scalable_dimension = aws_appautoscaling_target.streaming.scalable_dimension
  service_namespace  = aws_appautoscaling_target.streaming.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = var.autoscaling_target_cpu
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
