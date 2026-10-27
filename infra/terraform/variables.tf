variable "project_name" {
  type    = string
  default = "softexpert-document-exporter"
}

variable "aws_region" {
  type    = string
  default = "sa-east-1"
}

variable "vpc_id" {
  type = string
}

variable "app_subnet_id" {
  type = string
}

variable "db_subnet_ids" {
  type        = list(string)
  description = "At least two subnets in different AZs for RDS subnet group."
}

variable "ami_id" {
  type        = string
  description = "Amazon Linux 2023 AMI ID validated by the customer."
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "root_volume_size_gb" {
  type    = number
  default = 30
}

variable "api_ingress_cidrs" {
  type        = list(string)
  default     = []
  description = "CIDRs allowed to reach HTTP/HTTPS. Prefer ALB/VPN/private CIDRs."
}

variable "db_name" {
  type    = string
  default = "exporter"
}

variable "db_username" {
  type    = string
  default = "exporter"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "tags" {
  type    = map(string)
  default = {}
}
