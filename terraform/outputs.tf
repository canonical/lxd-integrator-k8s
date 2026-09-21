output "app_name" {
  description = "Name of the deployed integrator Juju application."
  value       = juju_application.lxd_integrator_k8s.name
}

output "provides" {
  description = "Map of relation names provided by the integrator."
  value = {
    https = "https"
  }
}

output "requires" {
  description = "Map of relation names required by the integrator."
  value       = {}
}
