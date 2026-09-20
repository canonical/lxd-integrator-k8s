# LXD Integrator K8s

A workload-less Kubernetes charm that holds administrative credentials for
unmanaged LXD servers and publishes connection information to requirers over
the `lxd-https` interface.

## Overview

`lxd-integrator-k8s` does not run a workload container. Instead, it stores LXD
client credentials in a Juju secret, registers the client certificates of
requiring charms in the LXD trust store, and exposes LXD endpoint addresses,
projects, and certificate material through the `lxd-https` relation. This lets
workload charms consume an existing LXD cluster without embedding privileged
credentials themselves.

## Deploy

Build the charm, then deploy it as a standard Kubernetes charm:

```bash
charmcraft pack
juju deploy ./lxd-integrator-k8s_*.charm
```

The charm is workload-less, so there is no workload container or rock to manage.

## Configuration

| Option                 | Type    | Default         | Meaning |
| ---------------------- | ------- | --------------- | ------- |
| `lxd-endpoints`        | string  |                 | Comma-separated list of LXD HTTPS API endpoints, most-preferred first. IPv6 literals must be bracketed, for example `[::1]:8443`. Port 8443 is used when omitted. Do not include a URL scheme; HTTPS is implied. |
| `lxd-credentials`      | secret  |                 | Juju secret that contains the integrator's LXD credentials. See [Credentials and trust-store setup](#credentials-and-trust-store-setup). |
| `lxd-server-fingerprint` | string |               | SHA-256 fingerprint (hex) of the LXD server's TLS certificate. |
| `project`              | string  |                 | LXD project requirers operate in. It is published over `lxd-https`, and a requirer places every instance, image, network and operation it creates in it. The project must already exist; the integrator does not create it. Setting it also restricts the requirer's trust-store entry to that project, so LXD enforces the isolation rather than the requirer's good behaviour. Leave unset to have requirers use the LXD `default` project. |
| `default-projects`     | string  |                 | Comma-separated list of default LXD projects used as the fallback project restriction when adding a requirer's certificate to LXD. It applies only when the requirer publishes no projects list of its own and `project` is unset. Leave unset to allow requirers all-project access. |
| `trust-name-prefix`    | string  | `juju-relation` | Prefix used when registering trusted client certificates in LXD. |

## Credentials and trust-store setup

Create a Juju secret that holds the integrator's LXD client credentials. The
secret must contain `client-cert` and `client-key` together, and must contain
either the `server-cert` key or be paired with the `lxd-server-fingerprint`
configuration option. In other words, exactly one of the following must be
provided:

- the `server-cert` key in the `lxd-credentials` secret, or
- the `lxd-server-fingerprint` configuration option.

The `trust-token` key in the secret is optional and is used once when adding a
trusted client to LXD.

```bash
juju add-secret lxd-credentials \
  client-cert="$(cat client.crt)" \
  client-key="$(cat client.key)" \
  server-cert="$(cat server.crt)"
# Capture the secret URI printed by the command.
```

Grant the secret to the integrator charm and configure the endpoints:

```bash
juju grant-secret <secret-uri> lxd-integrator-k8s
juju config lxd-integrator-k8s \
  lxd-endpoints="lxd-1.example.com:8443,lxd-2.example.com:8443" \
  lxd-credentials=<secret-uri>
```

The charm registers a trust-store entry for each requirer under a name built
from `trust-name-prefix`, this integrator's application name and model UUID,
and the remote application name, for example
`juju-relation-lxd-integrator-k8s-<model-uuid>-openshell-gateway`.
The `list-trusted-clients` action can be used to inspect the trust-store
entries owned by this charm.

## The `lxd-https` relation

This charm `provides` the `lxd-https` interface. Requirer charms integrate with
it to receive LXD connection information:

```bash
juju relate lxd-integrator-k8s <requiring-application>
```

The data published to requirers includes the configured endpoints, the server
certificate or fingerprint, and the `project` requirers are to operate in when
one is configured.

### Isolating requirers in their own project

For a production deployment, give the integrator a project of its own and let
it name that project to its requirers:

```bash
lxc project create openshell
juju config lxd-integrator-k8s project=openshell
```

The requirer then creates everything in `openshell`, and its trust-store entry
is restricted to `openshell` as well, so LXD refuses anything it attempts
elsewhere. Note that the driver reads its default network and storage pool
from the project's `default` profile, so lay that profile out before relating.

## Actions

| Action                  | Purpose |
| ----------------------- | ------- |
| `get-connection-info`   | Report whether the configured client certificate is trusted by LXD, the configured endpoint, the server certificate fingerprint, the addresses published to requirers, and the configured project. Fails cleanly when LXD is unreachable. |
| `list-trusted-clients` | List trust-store entries owned by this charm (matching `trust-name-prefix`). Fails cleanly when LXD is unreachable. |

## Security

- LXD endpoints configured in `lxd-endpoints` are published to requirers
  verbatim. The requirer's network must be able to reach those endpoints.
- The integrator holds administrative LXD credentials in a Juju secret. Grant
  that secret only to the integrator application.
- Communication to LXD is over HTTPS. Always verify the server through the
  `server-cert` secret key or the `lxd-server-fingerprint` option.
