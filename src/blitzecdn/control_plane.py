"""The composition root.

This is the one module that knows both halves: it builds the concrete adapters
from :mod:`blitzecdn.infrastructure` and injects them into the pure services in
:mod:`blitzecdn.application`. Production wiring lives here and nowhere else, so
"what does a real control plane consist of" is answerable by reading one
constructor.

``ControlPlane`` itself is a facade. It owns no logic — every method delegates
to the service that does — and exists so the CLI and the API keep a single
object to call, while each service underneath declares only the ports it uses.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.application import (
    CertificateService,
    DeploymentService,
    DnsService,
    EdgeOperationsService,
)
from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    CERTIFICATE_RENEWAL_DAYS,
    CacheStatsReport,
    CertificateInfo,
    CertificateStatus,
    Deployment,
    DnsRecord,
    Domain,
    DriftReport,
    HostDrift,
    OriginCheck,
    PreflightReport,
    PurgeEntry,
    PurgeResult,
    RecordPatch,
    RecordType,
)
from blitzecdn.infrastructure.ansible import AnsibleRunner
from blitzecdn.infrastructure.certificates import CertbotIssuer, CertificateStore
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.filesystem import atomic_write_yaml
from blitzecdn.infrastructure.inventory import Inventory
from blitzecdn.infrastructure.origins import OriginProbe
from blitzecdn.infrastructure.preflight import CertificatePreflight
from blitzecdn.ports import (
    CertificateStore as CertificateStorePort,
)
from blitzecdn.ports import (
    DeploymentRunner,
    EdgeInventory,
    Issuer,
    Preflight,
)
from blitzecdn.ports import (
    OriginProbe as OriginProbePort,
)


class ControlPlane:
    """Coordinate the application services at the process boundary."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository | None = None,
        runner: DeploymentRunner | None = None,
        certificate_store: CertificateStorePort | None = None,
        issuer: Issuer | None = None,
        origin_probe: OriginProbePort | None = None,
        preflight: Preflight | None = None,
        inventory: EdgeInventory | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings.database_path)
        self.runner = runner or AnsibleRunner(settings)
        self.certificate_store = certificate_store or CertificateStore(settings)
        self.issuer = issuer or CertbotIssuer(settings)
        self.origin_probe = origin_probe or OriginProbe(settings)
        self.preflight = preflight or CertificatePreflight(
            settings, origin_probe=self.origin_probe
        )
        self.inventory = inventory or Inventory(settings.inventory_path)

        # The repository satisfies every persistence port structurally, so one
        # object can serve four narrow dependencies without any service being
        # handed more of it than it asks for.
        self.dns = DnsService(self.repository, self.repository, self.repository)
        self.deployments = DeploymentService(
            settings,
            self.repository,
            self.repository,
            self.repository,
            self.runner,
            self.certificate_store,
            self.dns,
            atomic_write_yaml,
        )
        self.certificates = CertificateService(
            settings,
            self.repository,
            self.repository,
            self.runner,
            self.certificate_store,
            self.issuer,
            self.preflight,
            self.dns,
            self.deployments,
        )
        self.edges = EdgeOperationsService(
            settings,
            self.repository,
            self.repository,
            self.runner,
            self.origin_probe,
            self.inventory,
        )

    def initialize(self) -> int:
        return self.deployments.initialize()

    # -- Zones and records ---------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.dns.list_domains()

    def create_domain(self, domain: Domain, operator: str) -> Domain:
        return self.dns.create_domain(domain, operator)

    def delete_domain(self, name: str, operator: str) -> None:
        self.dns.delete_domain(name, operator)

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        return self.dns.list_records(domain)

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        return self.dns.get_record(domain, name, type_)

    def create_record(self, record: DnsRecord, operator: str) -> DnsRecord:
        return self.dns.create_record(record, operator)

    def update_record(
        self,
        domain: str,
        name: str,
        type_: RecordType,
        patch: RecordPatch,
        operator: str,
    ) -> DnsRecord:
        return self.dns.update_record(domain, name, type_, patch, operator)

    def set_proxied(
        self, domain: str, name: str, type_: RecordType, proxied: bool, operator: str
    ) -> DnsRecord:
        return self.dns.set_proxied(domain, name, type_, proxied, operator)

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        self.dns.delete_record(domain, name, type_, operator)

    def dns_export(self) -> list[dict[str, object]]:
        return self.dns.dns_export()

    # -- Certificates --------------------------------------------------

    def upload_certificate(
        self,
        name: str,
        certificate_pem: bytes,
        private_key_pem: bytes,
        operator: str,
    ) -> CertificateInfo:
        return self.certificates.upload_certificate(
            name, certificate_pem, private_key_pem, operator
        )

    def certificate_preflight(self, name: str) -> PreflightReport:
        return self.certificates.certificate_preflight(name)

    def request_certificate(
        self,
        name: str,
        operator: str,
        email: str | None = None,
        *,
        skip_preflight: bool = False,
    ) -> CertificateInfo:
        return self.certificates.request_certificate(
            name, operator, email, skip_preflight=skip_preflight
        )

    def reconcile_certificates(self, operator: str) -> dict[str, object]:
        return self.certificates.reconcile_certificates(operator)

    def certificate(self, name: str) -> CertificateInfo:
        return self.certificates.certificate(name)

    def certificate_statuses(self) -> list[CertificateStatus]:
        return self.certificates.certificate_statuses()

    def expiring_certificates(
        self, within_days: int = CERTIFICATE_RENEWAL_DAYS
    ) -> list[CertificateStatus]:
        return self.certificates.expiring_certificates(within_days)

    def renew_certificates(
        self,
        operator: str,
        *,
        within_days: int = CERTIFICATE_RENEWAL_DAYS,
        force: bool = False,
        sites: Sequence[str] | None = None,
    ) -> dict[str, list[str]]:
        return self.certificates.renew_certificates(
            operator, within_days=within_days, force=force, sites=sites
        )

    # -- Deployment ----------------------------------------------------

    def validate(self) -> list[str]:
        return self.deployments.validate()

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        return self.deployments.deploy(operator, check=check, host_limit=host_limit)

    def submit_deployment(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment:
        return self.deployments.submit_deployment(
            operator, check=check, host_limit=host_limit
        )

    def rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        return self.deployments.rollback(operator, deployment_id, check=check)

    def submit_rollback(
        self, operator: str, deployment_id: str | None = None, *, check: bool = False
    ) -> Deployment:
        return self.deployments.submit_rollback(operator, deployment_id, check=check)

    def check_drift(
        self, operator: str, *, host_limit: str | None = None
    ) -> DriftReport:
        return self.deployments.check_drift(operator, host_limit=host_limit)

    def drift_report(self, deployment_id: str) -> DriftReport:
        return self.deployments.drift_report(deployment_id)

    # -- Edge operations -----------------------------------------------

    def check_origins(self) -> list[OriginCheck]:
        return self.edges.check_origins()

    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult:
        return self.edges.purge_cache(
            operator, entries=entries, purge_all=purge_all, host_limit=host_limit
        )

    def cache_stats(
        self, operator: str, *, host_limit: str | None = None
    ) -> CacheStatsReport:
        return self.edges.cache_stats(operator, host_limit=host_limit)

    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostDrift, ...]:
        return self.edges.decommission_edge(name, operator, force=force)


def build_control_plane(settings: Settings) -> ControlPlane:
    """Build a control plane wired to the real adapters."""
    return ControlPlane(settings)
