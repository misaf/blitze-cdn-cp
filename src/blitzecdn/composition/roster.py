"""The application's features, the provider each one builds its services
with, and the behaviour each one binds.

Modules are named by path because importing them here would be a cycle. Each
row's provider and builders come from the capability's own `wiring.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from dishka import Provider

from blitzecdn.capabilities.backup.wiring import BackupProvider
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.cache.service import operations as cache_operations
from blitzecdn.capabilities.cache.wiring import CacheProvider
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.deployments.settings import DeploymentsConfig
from blitzecdn.capabilities.deployments.wiring import (
    DeploymentsProvider,
    deployments_operations,
)
from blitzecdn.capabilities.diagnostics.wiring import (
    DiagnosticsProvider,
    diagnostics_operations,
)
from blitzecdn.capabilities.dns.wiring import RENDERING as DNS_RENDERING
from blitzecdn.capabilities.dns.wiring import DnsProvider
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.capabilities.edges.service import operations as edge_operations
from blitzecdn.capabilities.edges.wiring import EdgesProvider
from blitzecdn.capabilities.http.http3.wiring import RENDERING as HTTP3_RENDERING
from blitzecdn.capabilities.http.wiring import RENDERING as HTTP_RENDERING
from blitzecdn.capabilities.jobs.ports import OperationRun
from blitzecdn.capabilities.jobs.wiring import JobsProvider, QueueReadiness
from blitzecdn.capabilities.maintenance.wiring import MaintenanceProvider
from blitzecdn.capabilities.origins.service import operations as origin_operations
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.origins.wiring import OriginsProvider
from blitzecdn.capabilities.releases.wiring import ReleasesProvider
from blitzecdn.capabilities.security.geoip.ports import DatabaseStore
from blitzecdn.capabilities.security.geoip.service import GeoIpService
from blitzecdn.capabilities.security.geoip.settings import GeoIpConfig
from blitzecdn.capabilities.security.geoip.wiring import (
    GeoIpProvider,
    geoip_operations,
    geoip_rendering,
)
from blitzecdn.capabilities.security.settings import SecurityConfig
from blitzecdn.capabilities.security.wiring import security_rendering
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import (
    TlsProvider,
    certificates_operations,
    certificates_rendering,
)
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.capabilities.traffic.wiring import TrafficProvider, traffic_operations
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.capabilities.workflows.wiring import WorkflowsProvider
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.features import (
    Feature,
    FeatureOperations,
    FeatureRegistry,
    FeatureRendering,
    load_features,
)
from blitzecdn.core.features.resolution import (
    ResolvedCapabilityEnvironment,
    resolve_policy_schema,
)

__all__ = [
    "BUILTIN_FEATURES",
    "ROSTER",
    "OperationInputs",
    "RenderingInputs",
    "RosterEntry",
    "bind_operations",
    "bind_queued_operations",
    "bind_renderings",
    "capability_providers",
    "compose_registry",
    "load_registry",
]


@dataclass(frozen=True, slots=True)
class RenderingInputs:
    """What rendering may read: configuration and adapters, never a service."""

    capability_config: ResolvedCapabilityEnvironment
    certificate_store: CertificateStore
    geoip_store: DatabaseStore


@dataclass(frozen=True, slots=True)
class OperationInputs:
    """What operations call, available once every service exists."""

    deployments: DeploymentService
    deployments_config: DeploymentsConfig
    certificates: CertificateService
    automatic_ssl: AutomaticSslService
    certificate_config: CertificateConfig
    workflows: WorkflowHistory
    queue_ready: QueueReadiness
    traffic: TrafficService
    traffic_config: TrafficConfig
    edges: EdgeStore
    geoip: GeoIpService
    geoip_config: GeoIpConfig
    cache: CacheService
    origins: OriginCheckService
    edge_operations: EdgeOperationsService


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One feature module, the provider of its services, and what it binds.

    ``provider`` builds the capability's stores and services; it is required
    of every capability that has a service. ``queued`` are the edge operations
    the API may queue and the worker runs.
    """

    module: str
    provider: type[Provider] | None = None
    rendering: Callable[[RenderingInputs], FeatureRendering[CdnSite]] | None = None
    operations: Callable[[OperationInputs], FeatureOperations] | None = None
    queued: Callable[[OperationInputs], Mapping[str, OperationRun]] | None = None


def _security_rendering(inputs: RenderingInputs) -> FeatureRendering[CdnSite]:
    return security_rendering(
        config=inputs.capability_config.for_feature("security").as_model(SecurityConfig)
    )


def _certificates_rendering(inputs: RenderingInputs) -> FeatureRendering[CdnSite]:
    return certificates_rendering(store=inputs.certificate_store)


def _geoip_rendering(inputs: RenderingInputs) -> FeatureRendering[CdnSite]:
    return geoip_rendering(store=inputs.geoip_store)


def _geoip_operations(inputs: OperationInputs) -> FeatureOperations:
    return geoip_operations(geoip=inputs.geoip, config=inputs.geoip_config)


def _deployments_operations(inputs: OperationInputs) -> FeatureOperations:
    return deployments_operations(
        deployments=inputs.deployments,
        drift_check_interval_seconds=(
            inputs.deployments_config.drift_check_interval_seconds
        ),
    )


def _diagnostics_operations(inputs: OperationInputs) -> FeatureOperations:
    return diagnostics_operations(
        read_database=lambda: inputs.workflows.list_workflows(1),
        queue_ready=inputs.queue_ready,
    )


def _certificates_operations(inputs: OperationInputs) -> FeatureOperations:
    return certificates_operations(
        certificates=inputs.certificates,
        automatic_ssl=inputs.automatic_ssl,
        config=inputs.certificate_config,
        deployment_timeout_seconds=inputs.deployments_config.timeout_seconds,
    )


def _traffic_operations(inputs: OperationInputs) -> FeatureOperations:
    return traffic_operations(
        traffic=inputs.traffic,
        config=inputs.traffic_config,
        has_edges=lambda: bool(inputs.edges.list_edges()),
        deployment_timeout_seconds=inputs.deployments_config.timeout_seconds,
    )


#: Stable registration and presentation order. Desired-state merging is
#: order-independent, so reordering rows changes no edge.
ROSTER: tuple[RosterEntry, ...] = (
    # The capability contracts first: nothing they contribute depends on
    # another capability being registered, and core composes their policy.
    RosterEntry(
        "blitzecdn.capabilities.http.feature",
        rendering=lambda _inputs: HTTP_RENDERING,
    ),
    RosterEntry(
        "blitzecdn.capabilities.dns.feature",
        provider=DnsProvider,
        rendering=lambda _inputs: DNS_RENDERING,
    ),
    RosterEntry(
        "blitzecdn.capabilities.edges.feature",
        provider=EdgesProvider,
        queued=lambda inputs: edge_operations.operation_handlers(
            inputs.edge_operations
        ),
    ),
    RosterEntry("blitzecdn.capabilities.jobs.feature", provider=JobsProvider),
    RosterEntry("blitzecdn.capabilities.releases.feature", provider=ReleasesProvider),
    RosterEntry("blitzecdn.capabilities.workflows.feature", provider=WorkflowsProvider),
    RosterEntry(
        "blitzecdn.capabilities.deployments.feature",
        provider=DeploymentsProvider,
        operations=_deployments_operations,
    ),
    RosterEntry(
        "blitzecdn.capabilities.tls.feature",
        provider=TlsProvider,
        rendering=_certificates_rendering,
        operations=_certificates_operations,
    ),
    RosterEntry(
        "blitzecdn.capabilities.maintenance.feature", provider=MaintenanceProvider
    ),
    RosterEntry(
        "blitzecdn.capabilities.diagnostics.feature",
        provider=DiagnosticsProvider,
        operations=_diagnostics_operations,
    ),
    RosterEntry("blitzecdn.capabilities.backup.feature", provider=BackupProvider),
    RosterEntry(
        "blitzecdn.capabilities.cache.feature",
        provider=CacheProvider,
        queued=lambda inputs: cache_operations.operation_handlers(inputs.cache),
    ),
    RosterEntry(
        "blitzecdn.capabilities.traffic.feature",
        provider=TrafficProvider,
        operations=_traffic_operations,
    ),
    RosterEntry("blitzecdn.capabilities.compression.feature"),
    RosterEntry(
        "blitzecdn.capabilities.security.feature",
        rendering=_security_rendering,
    ),
    RosterEntry(
        "blitzecdn.capabilities.origins.feature",
        provider=OriginsProvider,
        queued=lambda inputs: origin_operations.operation_handlers(inputs.origins),
    ),
    RosterEntry(
        "blitzecdn.capabilities.security.geoip.feature",
        provider=GeoIpProvider,
        rendering=_geoip_rendering,
        operations=_geoip_operations,
    ),
    RosterEntry("blitzecdn.capabilities.hardening.feature"),
    RosterEntry(
        "blitzecdn.capabilities.http.http3.feature",
        rendering=lambda _inputs: HTTP3_RENDERING,
    ),
    RosterEntry("blitzecdn.capabilities.resolver.feature"),
)

#: The feature modules alone, in roster order.
BUILTIN_FEATURES: tuple[str, ...] = tuple(entry.module for entry in ROSTER)


def load_registry() -> FeatureRegistry:
    """Every built-in feature's declarations, and the site policy they compose.

    Any failure is fatal.
    """
    return compose_registry(load_features(BUILTIN_FEATURES))


def compose_registry(features: Sequence[Feature]) -> FeatureRegistry:
    """A registry of ``features``, with the site policy composed from them.

    Composing the policy here, before any process builds a service, is what
    lets every model that carries one be declared at import.
    """
    registry = FeatureRegistry(features)
    SitePolicy.seal(resolve_policy_schema(registry.policy_contributions()))
    return registry


def bind_renderings(inputs: RenderingInputs) -> tuple[FeatureRendering[CdnSite], ...]:
    """Each row's rendering, in roster order."""
    return tuple(
        entry.rendering(inputs) for entry in ROSTER if entry.rendering is not None
    )


def bind_operations(inputs: OperationInputs) -> tuple[FeatureOperations, ...]:
    """Each row's operations, in roster order."""
    return tuple(
        entry.operations(inputs) for entry in ROSTER if entry.operations is not None
    )


def capability_providers() -> tuple[Provider, ...]:
    """Each row's provider, in roster order."""
    return tuple(entry.provider() for entry in ROSTER if entry.provider is not None)


def bind_queued_operations(inputs: OperationInputs) -> dict[str, OperationRun]:
    """Every queueable edge operation's run, by kind; a kind is owned once."""
    runs: dict[str, OperationRun] = {}
    for entry in ROSTER:
        if entry.queued is None:
            continue
        for kind, run in entry.queued(inputs).items():
            if kind in runs:
                raise ValueError(f"edge operation {kind!r} is queued twice")
            runs[kind] = run
    return runs
