set shell := ["bash", "-euo", "pipefail", "-c"]

# Pack the charm
build:
    charmcraft pack

# Run all checks (lint, unit, static)
check:
    tox

# Run linter and formatter checks
lint:
    tox -e lint

# Automatically format code
fmt:
    tox -e fmt

# Run unit tests
unit:
    tox -e unit

# Run static type checking
static:
    tox -e static

all: check
