#!/usr/bin/env python3

"""LXD Integrator K8s charm."""

from __future__ import annotations

import ipaddress
import logging

import ops

from config_model import PROJECT_NAME_RE, IntegratorConfig, load_config
from lxd_api import LxdApiError, LxdClient, LxdConnectionError, VerificationError, pem_fingerprint

logger = logging.getLogger(__name__)

HTTPS_RELATION = "https"
PEER_RELATION = "integrator-peers"


def endpoint_addresses(model: IntegratorConfig) -> list[str]:
    """Render ``model.endpoints`` as ``host:port`` strings (IPv6 bracketed)."""
    result: list[str] = []
    for host, port in model.endpoints:
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        if is_ip and ":" in host:
            result.append(f"[{host}]:{port}")
        else:
            result.append(f"{host}:{port}")
    return result


def is_clustered(model: IntegratorConfig) -> bool:
    """Return whether LXD is treated as clustered based on endpoint count."""
    return len(model.endpoints) > 1


class LxdIntegratorCharm(ops.CharmBase):
    """Workload-less integrator charm for unmanaged LXD servers.

    The charm implements the ``lxd-https`` provider: it publishes connection
    information to requirers and converges the LXD trust store on every hook.
    A single idempotent ``_reconcile`` derives desired state from live LXD and
    live model state; status is declared separately in
    ``collect-unit-status``.
    """

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self._model: IntegratorConfig | None = None
        self._config_error: str | None = None
        self._connection_factory = None

        credentials = self._resolve_credentials()
        raw_config = dict(self.config)
        raw_config.pop("lxd-credentials", None)
        raw_config.update(credentials)
        self._model, self._config_error = load_config(raw_config)

        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.secret_changed, self._reconcile)
        framework.observe(self.on.leader_elected, self._reconcile)
        framework.observe(self.on.update_status, self._reconcile)
        framework.observe(self.on[HTTPS_RELATION].relation_created, self._reconcile)
        framework.observe(self.on[HTTPS_RELATION].relation_changed, self._reconcile)
        framework.observe(self.on[HTTPS_RELATION].relation_broken, self._reconcile)
        framework.observe(self.on[PEER_RELATION].relation_created, self._reconcile)
        framework.observe(self.on[PEER_RELATION].relation_changed, self._reconcile)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        framework.observe(self.on.get_connection_info_action, self._on_get_connection_info)
        framework.observe(self.on.list_trusted_clients_action, self._on_list_trusted_clients)

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve the ``lxd-credentials`` secret into a key/value mapping.

        Returns a dictionary with the secret content keys
        (``client-cert``, ``client-key``, ``server-cert``, ``trust-token``), or
        an empty dictionary when the option is unset or the secret has not yet
        been granted.
        """
        secret_uri = self.config.get("lxd-credentials")
        if not secret_uri:
            return {}

        try:
            secret = self.model.get_secret(id=str(secret_uri))
            content = secret.get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            logger.warning("Failed to resolve lxd-credentials secret %s", secret_uri)
            return {}

        return {key: value for key, value in content.items() if value}

    def _build_client(self) -> LxdClient | None:
        """Build an ``LxdClient`` from the resolved config, or return ``None``."""
        if not self._model:
            return None
        if self._model.client_cert is None or self._model.client_key is None:
            return None
        params = self._model.to_connection_params()
        return LxdClient(
            **params.__dict__,
            connection_factory=self._connection_factory,
        )

    def _probe_trusted(self, client: LxdClient) -> bool:
        """Return whether ``client`` identifies as a trusted client certificate.

        Raises ``LxdConnectionError`` when LXD is unreachable and
        ``VerificationError`` when the server identity does not match the pin.
        An HTTP 403 or empty/mismatched identity is interpreted as untrusted.
        """
        try:
            identity = client.get_current_identity()
        except LxdApiError as exc:
            if exc.status == 403:
                return False
            raise
        identity_type = identity.get("type", "")
        return isinstance(identity_type, str) and identity_type.startswith("Client certificate")

    def _connection_databag(self) -> dict[str, str]:
        """Build the provider databag from config; no LXD call."""
        assert self._model is not None
        bag: dict[str, str] = {
            "version": "1.0",
            "certificate_fingerprint": self._server_fingerprint(),
            "addresses": ",".join(endpoint_addresses(self._model)),
        }
        if self._model.server_cert is not None:
            bag["certificate"] = self._model.server_cert
        if self._model.project is not None:
            bag["project"] = self._model.project
        return bag

    def _server_fingerprint(self) -> str:
        """Return the configured server fingerprint (config or derived from cert)."""
        assert self._model is not None
        if self._model.lxd_server_fingerprint is not None:
            return self._model.lxd_server_fingerprint.lower()
        assert self._model.server_cert is not None
        return pem_fingerprint(self._model.server_cert)

    def _publish_connection_info(self) -> None:
        """Publish the provider databag to every ``https`` relation."""
        if not self.model.unit.is_leader():
            return
        if not self._model:
            return
        bag = self._connection_databag()
        clustered = is_clustered(self._model)
        for relation in self.model.relations[HTTPS_RELATION]:
            if relation.app is None:
                continue
            target = relation.data[self.app] if clustered else relation.data[self.unit]
            target.update(bag)
            # update() never removes, so an optional key that has just been
            # unset in config would otherwise linger in the databag and keep
            # requirers pointed at a project the operator no longer wants.
            for optional in ("certificate", "project"):
                if optional not in bag:
                    target.pop(optional, None)

    def _trust_name(self, relation: ops.Relation) -> str:
        """Return the LXD trust entry name for a related application.

        The name is scoped to this integrator application so that multiple
        integrators targeting the same LXD do not classify each other's trust
        entries as owned.
        """
        assert self._model is not None
        assert relation.app is not None
        return (
            f"{self._model.trust_name_prefix}-"
            f"{self.app.name}-{self.model.uuid}-"
            f"{relation.app.name}"
        )

    def _ownership_prefix(self) -> str:
        """Return the name prefix that identifies this integrator's trust entries."""
        assert self._model is not None
        return f"{self._model.trust_name_prefix}-{self.app.name}-{self.model.uuid}-"

    def _is_charm_owned(self, entry: dict) -> bool:
        """Return whether a trust entry is owned by this integrator application."""
        if not self._model:
            return False
        name = entry.get("name", "")
        return isinstance(name, str) and name.startswith(self._ownership_prefix())

    def _read_relation_raw(self, relation: ops.Relation, key: str) -> str | None:
        """Read a raw string from the relation app bag, falling back to unit bags."""
        if relation.app is not None:
            value = relation.data[relation.app].get(key)
            if value:
                return value
        for unit in relation.units:
            value = relation.data[unit].get(key)
            if value:
                return value
        return None

    def _requirer_cert(self, relation: ops.Relation) -> str | None:
        """Read the requirer's published certificate from the relation."""
        return self._read_relation_raw(relation, "certificate")

    def _requirer_projects(self, relation: ops.Relation) -> list[str] | None:
        """Return the projects to restrict *relation*'s certificate to, or None.

        The operator's configuration is the upper bound. A requirer may ask for
        a subset of it — an application that only needs one of several allowed
        projects has no business holding trust in the rest — but never for a
        project the operator did not allow, and never for no restriction at
        all. Both of those are writes the requirer makes to its own databag,
        and either would hand it an LXD credential wider than the one the
        operator described.

        Returns None when the request cannot be honoured, so the caller can
        leave the trust store alone and say why rather than registering
        something the operator did not ask for.
        """
        assert self._model is not None
        allowed = self._model.parsed_projects
        raw = self._read_relation_raw(relation, "projects")
        if not raw:
            return allowed

        requested = [part.strip() for part in raw.split(",") if part.strip()]
        invalid = [name for name in requested if not PROJECT_NAME_RE.fullmatch(name)]
        if invalid or not requested:
            logger.warning(
                "Relation %s asked for an unusable project list %r; using the configured %r",
                relation.id,
                raw,
                allowed,
            )
            return allowed

        if not allowed:
            # The operator restricted nothing, so there is no upper bound to
            # narrow to and the requirer's own list is the only restriction
            # anyone asked for. Honouring it can only reduce its reach.
            return requested

        narrowed = [name for name in requested if name in allowed]
        if not narrowed:
            logger.warning(
                "Relation %s asked for projects %r, none of which the operator allows (%r); "
                "not registering its certificate",
                relation.id,
                requested,
                allowed,
            )
            return None
        if len(narrowed) != len(requested):
            logger.warning(
                "Relation %s asked for projects %r; narrowing to the configured %r",
                relation.id,
                requested,
                narrowed,
            )
        return narrowed

    def _converge_trust(
        self,
        client: LxdClient,
        broken_relation_id: int | None = None,
    ) -> None:
        """Register related certs and revoke orphaned or rotated charm-owned entries."""
        assert self._model is not None
        trust_entries = client.list_trusted_certificates()

        known_fingerprints = {entry.get("fingerprint", "").lower() for entry in trust_entries}
        expected_by_name: dict[str, str] = {}

        for relation in self.model.relations[HTTPS_RELATION]:
            if relation.app is None:
                continue
            if broken_relation_id is not None and relation.id == broken_relation_id:
                continue
            cert = self._requirer_cert(relation)
            if not cert:
                continue
            try:
                fp = pem_fingerprint(cert)
            except ValueError:
                logger.warning(
                    "Skipping relation %s: requirer certificate is not valid PEM",
                    relation.id,
                )
                continue
            projects = self._requirer_projects(relation)
            if projects is None:
                # Asked for reach the operator does not allow. Leaving the name
                # out of expected_by_name also revokes an entry registered
                # before the request changed.
                continue
            name = self._trust_name(relation)
            expected_by_name[name] = fp
            if fp in known_fingerprints:
                # The entry exists, but the project restriction is config and
                # config changes. Without this the isolation an operator asks
                # for by setting `project` never reaches LXD on an existing
                # relation, and the entry silently keeps its old reach.
                self._converge_trust_projects(client, trust_entries, fp, projects)
                continue
            client.add_trusted_certificate(
                cert,
                name=name,
                projects=projects,
                trust_token=self._model.trust_token,
            )

        for entry in trust_entries:
            if not self._is_charm_owned(entry):
                continue
            name = entry.get("name", "")
            entry_fp = entry.get("fingerprint", "").lower()
            if not isinstance(name, str):
                continue
            expected_fp = expected_by_name.get(name)
            if expected_fp is None or expected_fp != entry_fp:
                client.remove_trusted_certificate(entry_fp)

    def _converge_trust_projects(
        self,
        client: LxdClient,
        trust_entries: list[dict],
        fingerprint: str,
        projects: list[str],
    ) -> None:
        """Bring an existing entry's project restriction in line with config."""
        entry = next(
            (e for e in trust_entries if e.get("fingerprint", "").lower() == fingerprint),
            None,
        )
        if entry is None or not self._is_charm_owned(entry):
            return

        current = entry.get("projects") or []
        restricted = bool(entry.get("restricted"))
        if sorted(current) == sorted(projects) and restricted == bool(projects):
            return

        logger.info(
            "Updating trust entry %s: projects %s -> %s",
            entry.get("name", fingerprint),
            current,
            projects,
        )
        client.set_trusted_certificate_projects(fingerprint, projects)

    def _reconcile(self, event: ops.EventBase) -> None:
        """Idempotently publish connection info and converge the LXD trust store."""
        self._publish_connection_info()

        client = self._build_client()
        if client is None:
            logger.info("Skipping trust convergence: LXD credentials incomplete")
            return

        broken_relation_id: int | None = None
        for relation in self.model.relations[HTTPS_RELATION]:
            if relation.app is None:
                continue
            if isinstance(event, ops.RelationBrokenEvent) and event.relation.id == relation.id:
                broken_relation_id = relation.id
                break

        try:
            self._converge_trust(client, broken_relation_id)
        except (LxdConnectionError, VerificationError, LxdApiError) as exc:
            logger.warning("Skipping trust convergence: %s", exc)

    def _readiness_gaps(self) -> list[str]:
        """Return human-readable reasons the charm is not active."""
        gaps: list[str] = []
        if self._config_error:
            gaps.append(self._config_error)
        if not self._model:
            return gaps
        if not self._model.endpoints:
            gaps.append("no LXD endpoints configured")
        if self._model.client_cert is None or self._model.client_key is None:
            gaps.append("LXD credentials incomplete")
        if self._model.lxd_server_fingerprint is None and self._model.server_cert is None:
            gaps.append("no server certificate or fingerprint configured")
        return gaps

    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        """Declare unit status from live model and LXD state."""
        gaps = self._readiness_gaps()
        if gaps:
            event.add_status(ops.BlockedStatus(", ".join(gaps)))
            return

        client = self._build_client()
        if client is None:
            event.add_status(ops.BlockedStatus("LXD credentials incomplete"))
            return

        try:
            trusted = self._probe_trusted(client)
        except LxdConnectionError:
            event.add_status(ops.WaitingStatus("LXD unreachable"))
            return
        except VerificationError as exc:
            event.add_status(ops.BlockedStatus(f"server identity mismatch: {exc}"))
            return
        except LxdApiError as exc:
            event.add_status(ops.BlockedStatus(f"LXD API error: {exc}"))
            return

        if not trusted:
            event.add_status(ops.BlockedStatus("LXD credentials not trusted"))
            return

        event.add_status(ops.ActiveStatus("Ready"))

    def _on_get_connection_info(self, event: ops.ActionEvent) -> None:
        """Report trust status and published connection information."""
        if self._model is None:
            event.fail("configuration error")
            return

        client = self._build_client()
        if client is None:
            event.fail("LXD credentials incomplete")
            return

        try:
            trusted = self._probe_trusted(client)
        except LxdConnectionError as exc:
            event.fail(f"LXD unreachable: {exc}")
            return
        except VerificationError as exc:
            event.fail(f"server identity mismatch: {exc}")
            return
        except LxdApiError as exc:
            event.fail(f"LXD API error: {exc}")
            return

        addresses = endpoint_addresses(self._model)
        if not addresses:
            event.fail("no LXD endpoints configured")
            return

        event.set_results(
            {
                "trusted": "true" if trusted else "false",
                "endpoint": addresses[0],
                "certificate-fingerprint": self._server_fingerprint(),
                "addresses": ",".join(addresses),
                "project": self._model.project or "",
            }
        )

    def _on_list_trusted_clients(self, event: ops.ActionEvent) -> None:
        """List charm-owned trusted client certificates."""
        client = self._build_client()
        if client is None:
            event.fail("LXD credentials incomplete")
            return

        try:
            entries = client.list_trusted_certificates()
        except LxdConnectionError as exc:
            event.fail(f"LXD unreachable: {exc}")
            return
        except VerificationError as exc:
            event.fail(f"server identity mismatch: {exc}")
            return
        except LxdApiError as exc:
            event.fail(f"LXD API error: {exc}")
            return

        # Keyed by position rather than collected into a list: ops flattens a
        # nested mapping into the dotted keys Juju's action results use, but
        # stringifies a list, which turned this output into a Python repr that
        # is neither YAML nor JSON and cannot be read by a script.
        owned = {
            str(index): {
                "name": entry.get("name", ""),
                "fingerprint": entry.get("fingerprint", ""),
                # The projects an entry is restricted to are the whole point of
                # the `project` option, so an operator has to be able to see
                # them without reaching for the LXD CLI.
                "restricted": "true" if entry.get("restricted") else "false",
                "projects": ",".join(entry.get("projects") or []),
            }
            for index, entry in enumerate(
                entry for entry in entries if self._is_charm_owned(entry)
            )
        }
        event.set_results({"clients": owned, "count": str(len(owned))})


if __name__ == "__main__":
    ops.main(LxdIntegratorCharm)
