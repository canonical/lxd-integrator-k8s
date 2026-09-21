resource "juju_application" "lxd_integrator_k8s" {
  model_uuid = var.model_uuid
  name       = var.app_name
  units      = var.units

  charm {
    name     = "lxd-integrator-k8s"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  constraints = var.constraints
  config      = var.config
}
