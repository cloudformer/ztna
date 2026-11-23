variable "region" {
  type    = string
  default = "us-east-2"
}
variable "aws_profile" {
  description = "Local AWS CLI profile; \"\" = default credential chain"
  type        = string
  default     = ""
}
variable "assume_role_arn" {
  type    = string
  default = ""
}
variable "env" {
  type    = string
  default = "dev"
}
variable "project_name" {
  type    = string
  default = "ztna"
}
variable "networks" {
  type = map(object({
    subnets            = list(string)
    security_group_ids = list(string)
    public             = optional(bool, false)
  }))
}
variable "default_network" {
  type    = string
  default = ""
}
variable "targets" {
  type    = list(string)
  default = []
}
variable "http_auth" {
  type = object({
    enabled  = bool
    username = optional(string, "")
    password = optional(string, "")
  })
  default   = { enabled = false }
  sensitive = true
}
variable "cloudflare_auth" {
  type = object({
    enabled = bool
    team    = optional(string, "")
    aud     = optional(string, "")
  })
  default = { enabled = false }
}
variable "ztna_version" {
  description = "The ztna release to deploy from PyPI. Bump this per release during testing; the module is not touched."
  type        = string
  default     = "0.1.0"
}
variable "instance_type" {
  type    = string
  default = "t4g.micro"
}
variable "architecture" {
  type    = string
  default = "arm64"
}
variable "max_jumps" {
  type    = number
  default = 3
}
variable "idle_shutdown_minutes" {
  type    = number
  default = 30
}
variable "max_session_minutes" {
  type    = number
  default = 480
}
variable "function_url_auth" {
  type    = string
  default = "NONE"
}
variable "log_retention_days" {
  type    = number
  default = 14
}
variable "force_destroy" {
  type    = bool
  default = true
}
