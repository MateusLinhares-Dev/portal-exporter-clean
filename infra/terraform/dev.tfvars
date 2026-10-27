project_name = "softexpert-portal-exporter-dev"

aws_region    = "sa-east-1"
vpc_id        = "vpc-REAL"
app_subnet_id = "subnet-REAL"

db_subnet_ids = [
  "subnet-A",
  "subnet-B"
]

ami_id        = "ami-AMAZON-LINUX-2023"
instance_type = "t3.medium"

api_ingress_cidrs = []

tags = {
  Environment = "dev"
  Customer    = "Falcao-Bauer"
}
