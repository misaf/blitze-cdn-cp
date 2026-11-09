"""Which capability's object satisfies another capability's port.

A capability declares what it needs as a port of its own and its provider asks
for that port; it never names another capability's service. Where the declared
capability graph lets the owner provide a consumer's port, the owner's provider
does it. Every other port is bound here, to the one object that satisfies it.
Nothing here constructs a service.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from dishka import Provider, Scope, alias, provide

from blitzecdn.capabilities.automatic_ssl.ports import SslModeUpgrades
from blitzecdn.capabilities.backup.service import BackupService
from blitzecdn.capabilities.cache.ports import CacheUsage
from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
from blitzecdn.capabilities.deployments.ports import (
    ConvergenceOutcomes,
    DeploymentRequirements,
    DeploymentRunner,
    Releases,
    SettingRestore,
    Workflows,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.deployments.settings import DeploymentsConfig
from blitzecdn.capabilities.diagnostics.ports import (
    BackupHistory,
    ConvergenceHistory,
    QueueHistory,
)
from blitzecdn.capabilities.dns.service import DnsService, HostService
from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.capabilities.edges.ports import OriginProbe as EdgeOriginProbe
from blitzecdn.capabilities.jobs.ports import JobStore
from blitzecdn.capabilities.maintenance.ports import Deployments
from blitzecdn.capabilities.origins.adapters import OriginCheckPlaybook
from blitzecdn.capabilities.origins.ports import OriginCheckRunner
from blitzecdn.capabilities.origins.ports import OriginProbe as OriginsOriginProbe
from blitzecdn.capabilities.origins.ports import SiteReader as OriginsSiteReader
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.security.geoip.ports import (
    DeploymentLock,
    DeploymentRequirement,
)
from blitzecdn.capabilities.security.geoip.ports import (
    ReleaseHistory as GeoIpReleases,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    CertbotTimeout,
    RecordReader,
    SiteEditor,
    SiteWriter,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    ReleaseHistory as TlsReleases,
)
from blitzecdn.capabilities.tls.certificates.ports import (
    WorkflowCoordinator as TlsWorkflows,
)
from blitzecdn.capabilities.traffic.ports import EdgeNames
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.ports.operations import PlaybookRunner

__all__ = ["PortBindings"]


class PortBindings(Provider):
    """Each cross-capability port, bound to what satisfies it."""

    scope = Scope.APP

    # -- releases are compiled once, and read by whoever pins material.
    deployment_releases = alias(source=ReleaseService, provides=Releases)
    certificate_releases = alias(source=ReleaseService, provides=TlsReleases)
    geoip_releases = alias(source=ReleaseService, provides=GeoIpReleases)

    # -- one workflow coordinator, behind each capability's narrow protocol.
    deployment_workflows = alias(source=WorkflowCoordinator, provides=Workflows)

    @provide
    def certificate_workflows(self, workflows: WorkflowCoordinator) -> TlsWorkflows:
        # ``@contextmanager`` exposes its concrete private context-manager
        # type to mypy. The behavior is the narrow protocol TLS declares.
        return cast(TlsWorkflows, workflows)

    # -- DNS owns where a host's policy comes from.
    site_writer = alias(source=HostService, provides=SiteWriter)
    record_reader = alias(source=DnsService, provides=RecordReader)
    origin_sites = alias(source=HostService, provides=OriginsSiteReader)

    # -- TLS decides what an Automatic SSL/TLS upgrade writes, and where.
    ssl_mode_upgrades = alias(source=SiteEditor, provides=SslModeUpgrades)

    # -- the deployment lock, its budget and its durable reasons.
    geoip_lock = alias(source=DeploymentRunner, provides=DeploymentLock)
    maintenance_deployments = alias(source=DeploymentService, provides=Deployments)
    convergence_history = alias(source=ConvergenceOutcomes, provides=ConvergenceHistory)
    setting_restore = alias(source=AnsibleSettingStore, provides=SettingRestore)

    @provide
    def certbot_timeout(self, config: DeploymentsConfig) -> CertbotTimeout:
        # Issuance runs under the deployment lock, so it shares its budget.
        return CertbotTimeout(config.timeout_seconds)

    @provide
    def geoip_requirement(
        self, requirements: DeploymentRequirements
    ) -> DeploymentRequirement:
        return lambda: requirements.require(DeploymentRequirementKind.GEOIP_DATABASE)

    # -- the fleet, as the capabilities that run plays against it read it.
    origin_probe = alias(source=EdgeOriginProbe, provides=OriginsOriginProbe)

    @provide
    def origin_checks(self, fleet: PlaybookRunner) -> OriginCheckRunner:
        return OriginCheckPlaybook(fleet)

    @provide
    def edge_names(self, edges: EdgeStore) -> EdgeNames:
        def names() -> Sequence[str]:
            return [edge.name for edge in edges.list_edges()]

        return names

    # -- what reporting reads.
    cache_usage = alias(source=TrafficService, provides=CacheUsage)
    queue_history = alias(source=JobStore, provides=QueueHistory)
    backup_history = alias(source=BackupService, provides=BackupHistory)
