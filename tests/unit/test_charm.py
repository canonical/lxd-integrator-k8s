"""Unit tests for the integrator charm entrypoint."""

from __future__ import annotations

import hashlib
import json

import pytest
from ops.testing import Context, Model, Relation, Secret, State

from charm import LxdIntegratorCharm
from lxd_api import pem_fingerprint

APP_NAME = "lxd-integrator-k8s"
MODEL_UUID = "test-model-uuid"


def _expected_trust_name(remote_app_name: str) -> str:
    return f"juju-relation-{APP_NAME}-{MODEL_UUID}-{remote_app_name}"


def _credentials_secret(client_cert: str, client_key: str) -> Secret:
    return Secret(
        id="secret:0123456789abcdef0123",
        tracked_content={"client-cert": client_cert, "client-key": client_key},
    )


def _state(
    client_cert: str,
    client_key: str,
    server_fingerprint: str,
    relations: set[Relation] | None = None,
    leader: bool = False,
    **config_overrides: object,
) -> State:
    config: dict[str, object] = {
        "lxd-endpoints": "localhost",
        "lxd-server-fingerprint": server_fingerprint,
        "lxd-credentials": "secret:0123456789abcdef0123",
    }
    config.update(config_overrides)
    secret = _credentials_secret(client_cert, client_key)
    return State(
        config=config,
        secrets={secret},
        relations=relations or set(),
        leader=leader,
        model=Model(uuid=MODEL_UUID),
    )


def _run_reconcile(
    ctx: Context,
    state: State,
    transport,
) -> State:
    with ctx._run(ctx.on.config_changed(), state) as ops:
        ops.charm._connection_factory = transport
        ops.run()
    return ops.state


@pytest.fixture
def base_state(cert_pair):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()

    def make(relations=None, leader=False, **overrides):
        return _state(
            cert_pem, key_pem, fingerprint, relations=relations, leader=leader, **overrides
        )

    return make


def test_charm_starts(cert_pair):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = _state(cert_pem, key_pem, fingerprint)
    with ctx._run(ctx.on.start(), state) as ops:
        ops.run()
        charm = ops.charm

    assert charm._model is not None
    assert charm._config_error is None
    assert charm._model.client_cert == cert_pem
    assert charm._model.client_key == key_pem


def test_missing_secret_is_config_error():
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = State(config={"lxd-endpoints": "localhost"})
    with ctx._run(ctx.on.start(), state) as ops:
        ops.run()
        charm = ops.charm

    assert charm._model is None
    assert charm._config_error is not None
    assert not charm._config_error.startswith("Value error")


def test_publish_single_endpoint_to_unit_bag(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert rel_out.local_unit_data["version"] == "1.0"
    assert "certificate_fingerprint" in rel_out.local_unit_data
    assert rel_out.local_unit_data["addresses"] == "localhost:8443"
    assert rel_out.local_app_data == {}


def test_publish_clustered_to_app_bag(cert_pair, transport):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = _state(
        cert_pem,
        key_pem,
        fingerprint,
        relations={relation},
        leader=True,
        **{"lxd-endpoints": "10.0.0.1:8443, 10.0.0.2:8443"},
    )
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert rel_out.local_app_data["addresses"] == "10.0.0.1:8443,10.0.0.2:8443"
    assert "addresses" not in rel_out.local_unit_data


def test_publish_fingerprint_only_omits_certificate(cert_pair, transport):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = _state(cert_pem, key_pem, fingerprint, relations={relation}, leader=True)
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert "certificate" not in rel_out.local_unit_data
    assert rel_out.local_unit_data["certificate_fingerprint"] == fingerprint


def test_register_requirer_certificate(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    requirer_cert = requirer_cert.strip()
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert},
    )
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    _run_reconcile(ctx, state, tr)

    post = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]
    assert len(post) == 1
    body = json.loads(post[0][2] or b"{}")
    assert body["name"] == _expected_trust_name("openshell-gateway")


def test_register_idempotent_on_rerun(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    requirer_cert = requirer_cert.strip()
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert},
    )
    state = base_state(relations={relation}, leader=True)

    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    state_out = _run_reconcile(ctx, state, tr)
    posts_1 = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]
    assert len(posts_1) == 1

    tr2 = transport()
    tr2.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {
                "name": _expected_trust_name("openshell-gateway"),
                "fingerprint": pem_fingerprint(requirer_cert),
            },
        ],
    )
    _run_reconcile(ctx, state_out, tr2)
    posts_2 = [r for r in tr2.requests if r[:2] == ("POST", "/1.0/certificates")]
    assert len(posts_2) == 0


def test_revoke_on_relation_broken(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    requirer_cert = requirer_cert.strip()
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert},
    )
    state = base_state(relations={relation}, leader=True)

    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    state_out = _run_reconcile(ctx, state, tr)

    tr2 = transport()
    fp = pem_fingerprint(requirer_cert)
    tr2.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {"name": _expected_trust_name("openshell-gateway"), "fingerprint": fp},
        ],
    )
    tr2.add_response("DELETE", f"/1.0/certificates/{fp}", 200, {})
    relation_out = state_out.get_relation(relation.id)
    with ctx._run(ctx.on.relation_broken(relation_out), state_out) as ops:
        ops.charm._connection_factory = tr2
        ops.run()

    deletes = [r for r in tr2.requests if r[0] == "DELETE"]
    assert len(deletes) == 1


def test_non_charm_owned_entry_not_revoked(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = base_state(relations={relation}, leader=True)

    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates",
        200,
        [
            {"name": "manual-entry", "fingerprint": "aa" * 32},
        ],
    )
    _run_reconcile(ctx, state, tr)

    deletes = [r for r in tr.requests if r[0] == "DELETE"]
    assert len(deletes) == 0


def _register(cert_pair, transport, requirer_projects, **config):
    """Reconcile with one requirer and return the POSTed body, or None."""
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    remote_app_data = {"certificate": cert_pair[0]}
    if requirer_projects is not None:
        remote_app_data["projects"] = requirer_projects
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data=remote_app_data,
    )
    state = _state(
        cert_pem,
        key_pem,
        fingerprint,
        relations={relation},
        leader=True,
        **config,
    )
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    _run_reconcile(ctx, state, tr)

    posts = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]
    if not posts:
        return None
    return json.loads(posts[0][2] or b"{}")


def test_requirer_may_narrow_within_the_configured_projects(cert_pair, transport):
    body = _register(
        cert_pair,
        transport,
        "project-a",
        **{"default-projects": "project-a, project-b"},
    )
    assert body["projects"] == ["project-a"]
    assert body["restricted"] is True


def test_requirer_cannot_reach_outside_the_configured_projects(cert_pair, transport):
    # The requirer writes this key into its own databag, so honouring it would
    # let it choose how wide its own LXD credential is.
    body = _register(cert_pair, transport, "project-a, elsewhere", **{"project": "project-a"})
    assert body["projects"] == ["project-a"]
    assert body["restricted"] is True

    assert _register(cert_pair, transport, "elsewhere", **{"project": "project-a"}) is None


def test_requirer_cannot_remove_its_own_restriction(cert_pair, transport):
    # ``,`` is truthy but parses to an empty list, which LXD reads as "not
    # restricted" — the widest possible credential from the narrowest input.
    for raw in (",", " , ", ""):
        body = _register(cert_pair, transport, raw, **{"project": "openshell"})
        assert body["projects"] == ["openshell"], raw
        assert body["restricted"] is True, raw


def test_requirer_project_names_are_validated(cert_pair, transport):
    body = _register(
        cert_pair,
        transport,
        "project a/../other",
        **{"default-projects": "project-a"},
    )
    assert body["projects"] == ["project-a"]


def test_requirer_projects_apply_when_the_operator_restricted_nothing(cert_pair, transport):
    # With no upper bound there is nothing to narrow to, and honouring the
    # requirer's own list can only reduce the reach it would otherwise have.
    body = _register(cert_pair, transport, "project-a, project-b")
    assert body["projects"] == ["project-a", "project-b"]
    assert body["restricted"] is True


def test_an_unrestricted_entry_says_so_explicitly(cert_pair, transport):
    # LXD defaults ``restricted`` to false, so leaving the key out registered
    # an unrestricted certificate without the request ever saying that.
    body = _register(cert_pair, transport, None)
    assert body["projects"] == []
    assert body["restricted"] is False


def test_default_projects_parsed_and_restricted(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert.strip()},
    )
    state = base_state(
        relations={relation},
        leader=True,
        **{"default-projects": "project-a, project-b"},
    )
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    _run_reconcile(ctx, state, tr)

    post = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")][0]
    body = json.loads(post[2] or b"{}")
    assert body["projects"] == ["project-a", "project-b"]
    assert body["restricted"] is True


def test_malformed_requirer_cert_skipped(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": "not-valid-pem"},
    )
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    _run_reconcile(ctx, state, tr)

    posts = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]
    assert len(posts) == 0


def test_incomplete_credentials_build_client_none():
    secret = Secret(
        id="secret:0123456789abcdef0123",
        tracked_content={"client-cert": "CLIENT_CERT", "server-cert": "SERVER_CERT"},
    )
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = State(
        config={
            "lxd-endpoints": "localhost",
            "lxd-credentials": secret.id,
            "lxd-server-fingerprint": "aa" * 32,
        },
        secrets={secret},
    )
    with ctx._run(ctx.on.start(), state) as ops:
        ops.run()
        charm = ops.charm

    assert charm._build_client() is None
    assert ops.state.unit_status.name == "blocked"


def test_databag_published_when_lxd_unreachable(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    tr.raise_on_connect = True
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert rel_out.local_unit_data["addresses"] == "localhost:8443"


def test_lxd_api_error_skips_convergence(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    requirer_cert = "REQUIRER_CERT"
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert},
    )
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 500, {})
    _run_reconcile(ctx, state, tr)

    posts = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]
    assert len(posts) == 0


def test_get_connection_info_action_trusted(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 200, {"type": "Client certificate"})

    with ctx._run(ctx.on.action("get-connection-info"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ctx.action_results["trusted"] == "true"
    assert ctx.action_results["addresses"] == "localhost:8443"
    assert "certificate-fingerprint" in ctx.action_results


def test_get_connection_info_action_untrusted(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 403, {})

    with ctx._run(ctx.on.action("get-connection-info"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ctx.action_results["trusted"] == "false"


def test_get_connection_info_action_unreachable(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.raise_on_connect = True

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("get-connection-info"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "LXD unreachable" in str(exc_info.value)


def test_list_trusted_clients_action(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {"name": _expected_trust_name("openshell-gateway"), "fingerprint": "aa" * 32},
            {"name": "manual-entry", "fingerprint": "bb" * 32},
        ],
    )

    with ctx._run(ctx.on.action("list-trusted-clients"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    clients = ctx.action_results["clients"]
    assert ctx.action_results["count"] == "1"
    # Keyed by position so ops flattens it into Juju's dotted result keys; a
    # list here came back out as a Python repr.
    assert list(clients) == ["0"]
    assert clients["0"]["name"] == _expected_trust_name("openshell-gateway")


def test_list_trusted_clients_action_unreachable(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.raise_on_connect = True

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("list-trusted-clients"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "LXD unreachable" in str(exc_info.value)


def test_status_blocked_without_credentials():
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = State(config={"lxd-endpoints": "localhost"})
    state_out = ctx.run(ctx.on.start(), state)
    assert state_out.unit_status.name == "blocked"


def test_status_waiting_when_lxd_unreachable(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.raise_on_connect = True

    with ctx._run(ctx.on.update_status(), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ops.state.unit_status.name == "waiting"


def test_status_active_when_trusted(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 200, {"type": "Client certificate"})

    with ctx._run(ctx.on.update_status(), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ops.state.unit_status.name == "active"


def test_status_blocked_on_verification_error(base_state, transport):
    state = base_state(leader=True)
    tr = transport()
    # Use a mismatched server fingerprint to trigger VerificationError.
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state2 = State(
        config={
            "lxd-endpoints": "localhost",
            "lxd-server-fingerprint": "00" * 32,
            "lxd-credentials": state.config["lxd-credentials"],
        },
        secrets=state.secrets,
        leader=True,
    )

    with ctx._run(ctx.on.update_status(), state2) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ops.state.unit_status.name == "blocked"


def test_status_blocked_on_api_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 500, {})

    with ctx._run(ctx.on.update_status(), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ops.state.unit_status.name == "blocked"


def test_get_connection_info_action_ipv6_endpoint_bracketed(cert_pair, transport):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = _state(
        cert_pem,
        key_pem,
        fingerprint,
        leader=True,
        **{"lxd-endpoints": "[::1]:8443"},
    )
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 200, {"type": "Client certificate"})

    with ctx._run(ctx.on.action("get-connection-info"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    assert ctx.action_results["endpoint"] == "[::1]:8443"
    assert ctx.action_results["addresses"] == "[::1]:8443"


def test_get_connection_info_action_api_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/auth/identities/current", 500, {})

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("get-connection-info"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "LXD API error" in str(exc_info.value)


def test_get_connection_info_action_verification_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True, **{"lxd-server-fingerprint": "00" * 32})
    tr = transport()

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("get-connection-info"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "server identity mismatch" in str(exc_info.value)


def test_list_trusted_clients_action_api_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 500, {})

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("list-trusted-clients"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "LXD API error" in str(exc_info.value)


def test_list_trusted_clients_action_verification_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True, **{"lxd-server-fingerprint": "00" * 32})
    tr = transport()

    with (
        pytest.raises(Exception) as exc_info,
        ctx._run(ctx.on.action("list-trusted-clients"), state) as ops,
    ):
        ops.charm._connection_factory = tr
        ops.run()

    assert "server identity mismatch" in str(exc_info.value)


def test_reconcile_skips_on_verification_error(cert_pair, transport):
    cert_pem, key_pem, cert_der = cert_pair
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = _state(
        cert_pem,
        key_pem,
        fingerprint,
        relations={relation},
        leader=True,
        **{"lxd-server-fingerprint": "00" * 32},
    )
    tr = transport()

    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert rel_out.local_unit_data["addresses"] == "localhost:8443"
    assert len([r for r in tr.requests if r[:2] == ("GET", "/1.0/certificates?recursion=1")]) == 0
    assert len([r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")]) == 0


def test_project_published_to_requirer(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = base_state(relations={relation}, leader=True, project="openshell")
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert rel_out.local_unit_data["project"] == "openshell"


def test_project_absent_from_databag_when_unset(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(endpoint="https", remote_app_name="openshell-gateway")
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert "project" not in rel_out.local_unit_data


def test_unsetting_project_clears_it_from_the_databag(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        local_unit_data={"project": "openshell"},
    )
    state = base_state(relations={relation}, leader=True)
    tr = transport()
    state_out = _run_reconcile(ctx, state, tr)

    rel_out = state_out.get_relation(relation.id)
    assert "project" not in rel_out.local_unit_data


def test_project_restricts_the_trust_entry(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert.strip()},
    )
    state = base_state(
        relations={relation},
        leader=True,
        project="openshell",
        **{"default-projects": "project-a, project-b"},
    )
    tr = transport()
    tr.add_response("GET", "/1.0/certificates?recursion=1", 200, [])
    tr.add_response("POST", "/1.0/certificates", 200, {})
    _run_reconcile(ctx, state, tr)

    post = [r for r in tr.requests if r[:2] == ("POST", "/1.0/certificates")][0]
    body = json.loads(post[2] or b"{}")
    assert body["projects"] == ["openshell"]
    assert body["restricted"] is True


def test_invalid_project_is_a_config_error(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True, project="bad/project")
    with ctx._run(ctx.on.config_changed(), state) as ops:
        ops.charm._connection_factory = transport()
        ops.run()
        charm = ops.charm

    assert charm._model is None
    assert charm._config_error is not None
    assert "project" in charm._config_error


def test_changing_the_project_updates_an_existing_trust_entry(
    base_state, transport, requirer_cert
):
    # The entry is created before the project is configured, which is the
    # ordering an operator hits when they set `project` on a running
    # deployment. Without convergence the isolation never reaches LXD.
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert.strip()},
    )
    fingerprint = pem_fingerprint(requirer_cert.strip())
    state = base_state(relations={relation}, leader=True, project="openshell")
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {
                "name": _expected_trust_name("openshell-gateway"),
                "fingerprint": fingerprint,
                "projects": [],
                "restricted": False,
            }
        ],
    )
    tr.add_response("PATCH", f"/1.0/certificates/{fingerprint}", 200, {})
    _run_reconcile(ctx, state, tr)

    patches = [r for r in tr.requests if r[0] == "PATCH"]
    assert len(patches) == 1
    body = json.loads(patches[0][2] or b"{}")
    assert body["projects"] == ["openshell"]
    assert body["restricted"] is True
    assert not [r for r in tr.requests if r[0] == "DELETE"]


def test_a_matching_trust_entry_is_left_alone(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert.strip()},
    )
    fingerprint = pem_fingerprint(requirer_cert.strip())
    state = base_state(relations={relation}, leader=True, project="openshell")
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {
                "name": _expected_trust_name("openshell-gateway"),
                "fingerprint": fingerprint,
                "projects": ["openshell"],
                "restricted": True,
            }
        ],
    )
    _run_reconcile(ctx, state, tr)

    assert not [r for r in tr.requests if r[0] in ("PATCH", "POST", "DELETE")]


def test_another_charms_trust_entry_is_never_touched(base_state, transport, requirer_cert):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    relation = Relation(
        endpoint="https",
        remote_app_name="openshell-gateway",
        remote_app_data={"certificate": requirer_cert.strip()},
    )
    fingerprint = pem_fingerprint(requirer_cert.strip())
    state = base_state(relations={relation}, leader=True, project="openshell")
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {
                "name": "someone-elses-entry",
                "fingerprint": fingerprint,
                "projects": [],
                "restricted": False,
            }
        ],
    )
    _run_reconcile(ctx, state, tr)

    assert not [r for r in tr.requests if r[0] == "PATCH"]


def test_list_trusted_clients_reports_the_project_restriction(base_state, transport):
    # The restriction is the whole point of the project option, so it has to be
    # visible without reaching for the LXD CLI.
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True, project="openshell")
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [
            {
                "name": _expected_trust_name("openshell-gateway"),
                "fingerprint": "aa" * 32,
                "restricted": True,
                "projects": ["openshell"],
            }
        ],
    )

    with ctx._run(ctx.on.action("list-trusted-clients"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    client = ctx.action_results["clients"]["0"]
    assert client["projects"] == "openshell"
    assert client["restricted"] == "true"


def test_list_trusted_clients_reports_an_unrestricted_entry(base_state, transport):
    ctx = Context(LxdIntegratorCharm, app_name="lxd-integrator-k8s")
    state = base_state(leader=True)
    tr = transport()
    tr.add_response(
        "GET",
        "/1.0/certificates?recursion=1",
        200,
        [{"name": _expected_trust_name("openshell-gateway"), "fingerprint": "aa" * 32}],
    )

    with ctx._run(ctx.on.action("list-trusted-clients"), state) as ops:
        ops.charm._connection_factory = tr
        ops.run()

    client = ctx.action_results["clients"]["0"]
    assert client["projects"] == ""
    assert client["restricted"] == "false"
