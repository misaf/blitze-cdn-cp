"""One SQLite database, exposed as four focused stores.

``Repository`` composes them and satisfies every persistence port in
:mod:`blitzecdn.ports` structurally, so a caller that needs several stores can
still pass one object while each service declares only the slice it uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.deployments import Deployment, DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.snapshots import (
    SNAPSHOT_VERSION,
    decode_snapshot,
    decode_snapshot_zones,
    encode_snapshot,
)
from blitzecdn.infrastructure.stores import (
    AuditLog,
    Database,
    DeploymentStore,
    SiteStore,
    ZoneStore,
)

__all__ = [
    "SNAPSHOT_VERSION",
    "AuditLog",
    "Database",
    "DeploymentStore",
    "Repository",
    "SiteStore",
    "ZoneStore",
]


class Repository:
    """SQLite persistence with explicit transactions and immutable snapshots.

    A thin composition of the stores in :mod:`blitzecdn.infrastructure.stores`.
    The delegating methods exist so callers that legitimately want the whole of
    persistence — the composition root, the API's read endpoints, tests — keep
    one object to pass around; the services themselves take the narrow ports.
    """

    def __init__(self, path: Path) -> None:
        self.database = Database(path)
        self.sites = SiteStore(self.database)
        self.zones = ZoneStore(self.database)
        self.deployments = DeploymentStore(self.database, self.snapshot)
        self.audit_log = AuditLog(self.database)

    # -- Sites ---------------------------------------------------------

    def list_sites(self) -> list[CdnSite]:
        return self.sites.list_sites()

    def get_site(self, name: str) -> CdnSite:
        return self.sites.get_site(name)

    def create_site(self, site: CdnSite) -> CdnSite:
        return self.sites.create_site(site)

    def replace_site(self, site: CdnSite) -> CdnSite:
        return self.sites.replace_site(site)

    def delete_site(self, name: str) -> None:
        self.sites.delete_site(name)

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        self.sites.replace_all_sites(sites)

    # -- Zones and records ---------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.zones.list_domains()

    def get_domain(self, name: str) -> Domain:
        return self.zones.get_domain(name)

    def create_domain(self, domain: Domain) -> Domain:
        return self.zones.create_domain(domain)

    def delete_domain(self, name: str) -> None:
        self.zones.delete_domain(name)

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        return self.zones.list_records(domain)

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        return self.zones.get_record(domain, name, type_)

    def create_record(self, record: DnsRecord) -> DnsRecord:
        return self.zones.create_record(record)

    def replace_record(self, record: DnsRecord) -> DnsRecord:
        return self.zones.replace_record(record)

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None:
        self.zones.delete_record(domain, name, type_)

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None:
        self.zones.replace_all_records(domains, records)

    # -- Snapshots -----------------------------------------------------

    def snapshot(self) -> str:
        """Serialise the desired state a deployment converges and can roll back to."""
        return encode_snapshot(
            self.zones.list_domains(),
            self.zones.list_records(),
            self.sites.list_sites(),
        )

    decode_snapshot = staticmethod(decode_snapshot)
    decode_snapshot_zones = staticmethod(decode_snapshot_zones)

    # -- Deployments ---------------------------------------------------

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
        host_limit: str | None = None,
    ) -> Deployment:
        return self.deployments.create_deployment(
            operator,
            check_mode=check_mode,
            rollback_of=rollback_of,
            snapshot=snapshot,
            host_limit=host_limit,
        )

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment:
        return self.deployments.transition(deployment_id, expected, target, **values)

    def get_deployment(self, deployment_id: str) -> Deployment:
        return self.deployments.get_deployment(deployment_id)

    def deployment_snapshot(self, deployment_id: str) -> str:
        return self.deployments.deployment_snapshot(deployment_id)

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        return self.deployments.list_deployments(limit)

    def abandon_running(self) -> int:
        return self.deployments.abandon_running()

    def successful_rollback_target(self, current_snapshot: str) -> Deployment:
        return self.deployments.successful_rollback_target(current_snapshot)

    # -- Audit ---------------------------------------------------------

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return self.audit_log.audit(
            operator, action, resource_type, resource_id, details
        )

    def get_audit_event(self, event_id: int) -> AuditEvent:
        return self.audit_log.get_audit_event(event_id)

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        return self.audit_log.list_audit_events(limit)
