variable "aws_region" {
  default = "us-west-2"
}

variable "project" {
  default = "music-streaming"
}

variable "streaming_cpu" {
  default = 256
}

variable "streaming_memory" {
  default = 512
}

variable "cdn_cpu" {
  default = 256
}

variable "cdn_memory" {
  default = 512
}

variable "streaming_min_capacity" {
  default = 1
}

# Keep at 1 for Experiment 1 (single-instance saturation test).
# Set to 4 for Experiment 3 (horizontal scaling).
variable "streaming_max_capacity" {
  default = 1
}

variable "cdn_min_capacity" {
  default = 1
}

variable "autoscaling_target_cpu" {
  default = 70
}
