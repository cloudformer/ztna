terraform {
  required_version = ">= 1.3"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    archive  = { source = "hashicorp/archive", version = "~> 2.4" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile != "" ? var.aws_profile : null

  dynamic "assume_role" {
    for_each = var.assume_role_arn != "" ? [1] : []
    content {
      role_arn     = var.assume_role_arn
      session_name = "${var.project_name}-deploy"
    }
  }

  default_tags {
    tags = { project = var.project_name, env = var.env }
  }
}
