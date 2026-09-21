"""Unit tests for the pure configuration model."""

from __future__ import annotations

import pytest

from config_model import (
    DEFAULT_LXD_PORT,
    IntegratorConfig,
    LxdConnectionParams,
    VerificationMode,
    load_config,
    parse_endpoints,
)


def test_minimal_valid_config():
    cfg = IntegratorConfig.model_validate(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "CERT",
            "client-key": "KEY",
            "server-cert": "SERVER_CERT",
        }
    )
    assert cfg.trust_name_prefix == "juju-relation"
    assert cfg.verification_mode == VerificationMode.CERTIFICATE
    assert cfg.default_projects is None


def test_endpoint_normalisation():
    assert parse_endpoints("localhost") == (("localhost", DEFAULT_LXD_PORT),)
    assert parse_endpoints("host:8444") == (("host", 8444),)
    assert parse_endpoints("[::1]:8443") == (("::1", 8443),)
    assert parse_endpoints("a:8441, b:8442 ,[::3]") == (
        ("a", 8441),
        ("b", 8442),
        ("::3", DEFAULT_LXD_PORT),
    )


def test_endpoint_port_range_rejected():
    for invalid in ("host:0", "host:65536", "host:-1"):
        _, err = load_config(
            {
                "lxd-endpoints": invalid,
                "client-cert": "C",
                "client-key": "K",
                "server-cert": "S",
            }
        )
        assert err is not None
        assert "port" in err.lower()


def test_endpoint_scheme_and_unbracketed_ipv6_rejected():
    _, err = load_config(
        {
            "lxd-endpoints": "https://lxd.example.com:8443",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
        }
    )
    assert err is not None
    assert "scheme" in err.lower()

    _, err = load_config(
        {
            "lxd-endpoints": "2001:db8::1:8443",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
        }
    )
    assert err is not None
    assert "bracketed" in err.lower()


def test_credential_completeness():
    _, err = load_config(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "client-key": "K",
        }
    )
    assert "server-cert" in err
    assert "lxd-server-fingerprint" in err

    _, err = load_config(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
            "lxd-server-fingerprint": "F",
        }
    )
    assert "only one" in err.lower()

    _, err = load_config(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "server-cert": "S",
        }
    )
    assert "client-cert and client-key must be set together" in err


def test_to_connection_params():
    cfg = IntegratorConfig.model_validate(
        {
            "lxd-endpoints": "localhost:8444, [2001:db8::1]",
            "lxd-server-fingerprint": "AB:CD:EF",
            "client-cert": "CERT",
            "client-key": "KEY",
        }
    )
    params = cfg.to_connection_params()
    assert params == LxdConnectionParams(
        endpoints=(("localhost", 8444), ("2001:db8::1", DEFAULT_LXD_PORT)),
        verification_mode=VerificationMode.FINGERPRINT,
        server_pin="ab:cd:ef",
        client_cert="CERT",
        client_key="KEY",
    )


def test_load_config_error():
    model, err = load_config({"lxd-endpoints": "localhost"})
    assert model is None
    assert err is not None
    assert not err.startswith("Value error")


def test_empty_trust_name_prefix_rejected():
    _, err = load_config(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
            "trust-name-prefix": "   ",
        }
    )
    assert "trust-name-prefix cannot be empty" in err


def test_parsed_projects():
    cfg = IntegratorConfig.model_validate(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
            "default-projects": "a, b ,",
        }
    )
    assert cfg.parsed_projects == ["a", "b"]

    cfg_empty = IntegratorConfig.model_validate(
        {
            "lxd-endpoints": "localhost",
            "client-cert": "C",
            "client-key": "K",
            "server-cert": "S",
            "default-projects": "",
        }
    )
    assert cfg_empty.parsed_projects == []


_BASE = {
    "lxd-endpoints": "localhost",
    "client-cert": "CERT",
    "client-key": "KEY",
    "server-cert": "SERVER_CERT",
}


def test_project_defaults_to_none():
    cfg = IntegratorConfig.model_validate(dict(_BASE))
    assert cfg.project is None
    assert cfg.parsed_projects == []


def test_project_empty_string_is_none():
    cfg = IntegratorConfig.model_validate({**_BASE, "project": ""})
    assert cfg.project is None


@pytest.mark.parametrize("name", ["openshell", "a", "a" * 63, "proj-1_2.3"])
def test_project_accepts_lxd_names(name):
    cfg = IntegratorConfig.model_validate({**_BASE, "project": name})
    assert cfg.project == name


@pytest.mark.parametrize("name", ["bad/project", "a" * 64, "has space", "tab\t", "a\nb"])
def test_project_rejects_invalid_names(name):
    model, error = load_config({**_BASE, "project": name})
    assert model is None
    assert error is not None
    assert "project" in error


def test_project_wins_over_default_projects():
    cfg = IntegratorConfig.model_validate(
        {**_BASE, "project": "openshell", "default-projects": "a,b"}
    )
    assert cfg.parsed_projects == ["openshell"]


def test_default_projects_used_when_project_unset():
    cfg = IntegratorConfig.model_validate({**_BASE, "default-projects": "a, b"})
    assert cfg.parsed_projects == ["a", "b"]
