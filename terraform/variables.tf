variable "model_uuid" {
  description = "UUID of the Juju model in which to deploy the integrator."
  type        = string
}

variable "app_name" {
  description = "Name of the Juju application for the integrator."
  type        = string
  default     = "lxd-integrator-k8s"
}

variable "channel" {
  description = "Charm channel from which to deploy the integrator."
  type        = string
  default     = "latest/edge"
}

variable "revision" {
  description = "Charm revision to deploy. If unset, the latest revision from the channel is used."
  type        = number
  default     = null
}

variable "base" {
  description = "Base (OS series) on which to deploy the integrator."
  type        = string
  default     = "ubuntu@24.04"
}

variable "units" {
  description = <<-EOT
    Number of integrator units to deploy. The charm is workload-less and holds
    no state of its own beyond its peer relation, so additional units buy
    availability of the relation data rather than throughput.
  EOT
  type        = number
  default     = 1
}

variable "constraints" {
  description = "Juju constraints to apply to each integrator unit."
  type        = string
  default     = "arch=amd64"
}

variable "config" {
  description = <<-EOT
    Charm configuration options for the integrator. `lxd-credentials` must name
    a Juju secret granted to this application; Terraform does not create it,
    because the secret carries an administrative LXD client key that should not
    pass through state.
  EOT
  type        = map(string)
  default     = {}
}
