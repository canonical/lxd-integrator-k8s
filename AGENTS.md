# Agent Instructions for lxd-integrator-k8s

## Project Overview

This repository contains the `lxd-integrator-k8s` charm, a workload-less Kubernetes
operator charm that holds administrative LXD credentials for unmanaged LXD servers.
It registers requirer client certificates in the LXD trust store and publishes
connection information to requirers over the `lxd-https` interface.

## Repository Layout

| Path | Purpose |
|------|---------|
| `src/charm.py` | Main charm object, event handling and reconciliation |
| `src/config_model.py` | Pydantic v2 configuration model and validation (no ops imports) |
| `src/lxd_api.py` | Minimal TLS/REST client for the LXD API |
| `src/verification_mode.py` | TLS server verification strategy enum |
| `tests/unit/` | Unit tests (pytest, ops.testing) |
| `charmcraft.yaml` | Charm metadata, config, actions, and pack definitions |
| `pyproject.toml` | Python tooling configuration (pytest, ruff, pyright, coverage) |
| `tox.ini` | Test environments (lint, unit, static, fmt) |
| `Justfile` | Developer tasks (build, lint, fmt, unit, static) |
| `.github/workflows/` | CI/CD definitions (ci, release, promote) |

## Development Workflows

Run checks locally using `tox` or `just`:

```bash
# Run all checks (lint, unit, static)
tox
# or
just check

# Run individual checks
tox -e lint
tox -e unit
tox -e static

# Format code automatically
tox -e fmt
```

## Workflows and CI/CD

- `ci.yaml`: Runs lint, unit tests, static type checks, and packs the charm on pull requests.
- `release.yaml`: Runs checks, builds the charm, and publishes to Charmhub (`latest/edge`) on push to `main`.
- `promote.yaml`: Promotes charms between Charmhub channels/risks via `workflow_dispatch`.
