# Deploys the ztna module, which deploys a released ztna from PyPI (ztna_version).
# Before a Registry release, use the local source line.
module "ztna" {
  # source  = "cloudformer/infra/aws//modules/ztna"  # stable: a released version
  # version = "0.2.0"
  source = "../../terraform-aws-infra/modules/ztna" # local: this repo's working-tree module

  project_name          = var.project_name
  targets               = var.targets
  http_auth             = var.http_auth
  cloudflare_auth       = var.cloudflare_auth
  function_url_auth     = var.function_url_auth
  instance_type         = var.instance_type
  architecture          = var.architecture
  max_jumps             = var.max_jumps
  idle_shutdown_minutes = var.idle_shutdown_minutes
  max_session_minutes   = var.max_session_minutes
  log_retention_days    = var.log_retention_days
  force_destroy         = var.force_destroy
  ztna_version          = var.ztna_version
}

output "console_url" { value = module.ztna.console_url }
output "bucket" { value = module.ztna.bucket }
output "launch_template_id" { value = module.ztna.launch_template_id }
