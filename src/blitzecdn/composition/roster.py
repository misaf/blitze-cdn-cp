"""The application's features, and the behaviour each one binds.

Modules are named by path because importing them here would be a cycle. Each
row's builders call the capability's own `wiring.py` with named arguments.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.deployments.wiring import deployments_operations
from blitzecdn.capabilities.diagnostics.wiring import diagnostics_operations
from blitzecdn.capabilities.dns.wiring import RENDERING as DNS_RENDERING
from blitzecdn.capabilities.http.http3.wiring import RENDERING as HTTP3_RENDERING
from blitzecdn.capabilities.http.wiring import RENDERING as HTTP_RENDERING
from blitzecdn.capabilities.security.settings import SecurityConfig
from blitzecdn.capabilities.security.wiring import security_rendering
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import (
    certificates_operations,
    certificates_rendering,
)
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.capabilities.traffic.wiring import traffic_operations
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRegistry,
    FeatureRendering,
    load_features,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment

__all__ = [
    "BUILTIN_FEATURES",
    "ROSTER",
    "OperationInputs",
    "RenderingInputs",
    "RosterEntry",
    "bind_operations",
    "bind_renderings",
    "load_registry",
]


@dataclass(frozen=True, slots=True)
class RenderingInputs:
    """What rendering may read: configuration and adapters, never a service."""

    capability_config: ResolvedCapabilityEnvironment
    certificate_store: CertificateStore


@dataclass(frozen=True, slots=True)
class OperationInputs:
    """What operations call, available once every service exists."""

    settings: Settings
    deployments: DeploymentService
    certificates: CertificateService
    automatic_ssl: AutomaticSslService
    certificate_config: CertificateConfig
    read_database: Callable[[], object]
    queue_ready: Callable[[], bool]
    traffic: TrafficService
    traffic_config: TrafficConfig
    has_edges: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One feature module and the behaviour it binds, if any."""

    module: str
    rendering: Callable[[RenderingInputs], FeatureRendering[CdnSite]] | None = None
    operations: Callable[[OperationInputs], FeatureOperations] | None = None


def _security_rendering(inputs: RenderingInputs) -> FeatureRendering[CdnSite]:
    return security_rendering(
        config=inputs.capability_config.for_feature("security").as_model(SecurityConfig)
    )


def _certificates_rendering(inputs: RenderingInputs) -> FeatureRendering[CdnSite]:
    return certificates_rendering(store=inputs.certificate_store)


def _deployments_operations(inputs: OperationInputs) -> FeatureOperations:
    return deployments_operations(
        deployments=inputs.deployments,
        drift_check_interval_seconds=inputs.settings.drift_check_interval_seconds,
    )


def _diagnostics_operations(inputs: OperationInputs) -> FeatureOperations:
    return diagnostics_operations(
        read_database=inputs.read_database, queue_ready=inputs.queue_ready
    )


def _certificates_operations(inputs: OperationInputs) -> FeatureOperations:
    return certificates_operations(
        certificates=inputs.certificates,
        automatic_ssl=inputs.automatic_ssl,
        config=inputs.certificate_config,
        deployment_timeout_seconds=inputs.settings.deployment_timeout_seconds,
    )


def _traffic_operations(inputs: OperationInputs) -> FeatureOperations:
    return traffic_operations(
        traffic=inputs.traffic,
        config=inputs.traffic_config,
        has_edges=inputs.has_edges,
        deployment_timeout_seconds=inputs.settings.deployment_timeout_seconds,
    )


#: Stable registration and presentation order. Desired-state merging is
#: order-independent, so reordering rows changes no edge.
ROSTER: tuple[RosterEntry, ...] = (
    # The capability contracts first: nothing they contribute depends on
    # another capability being registered, and `dns` composes their policy.
    RosterEntry(
        "blitzecdn.capabilities.http.feature",
        rendering=lambda _inputs: HTTP_RENDERING,
    ),
    RosterEntry(
        "blitzecdn.capabilities.dns.feature",
        rendering=lambda _inputs: DNS_RENDERING,
    ),
    RosterEntry("blitzecdn.capabilities.edges.feature"),
    RosterEntry("blitzecdn.capabilities.jobs.feature"),
    RosterEntry("blitzecdn.capabilities.releases.feature"),
    RosterEntry("blitzecdn.capabilities.workflows.feature"),
    RosterEntry(
        "blitzecdn.capabilities.deployments.feature",
        operations=_deployments_operations,
    ),
    RosterEntry(
        "blitzecdn.capabilities.tls.feature",
        rendering=_certificates_rendering,
        operations=_certificates_operations,
    ),
    RosterEntry("blitzecdn.capabilities.maintenance.feature"),
    RosterEntry(
        "blitzecdn.capabilities.diagnostics.feature",
        operations=_diagnostics_operations,
    ),
    RosterEntry("blitzecdn.capabilities.backup.feature"),
    RosterEntry("blitzecdn.capabilities.cache.feature"),
    RosterEntry(
        "blitzecdn.capabilities.traffic.feature", operations=_traffic_operations
    ),
    RosterEntry("blitzecdn.capabilities.compression.feature"),
    RosterEntry(
        "blitzecdn.capabilities.security.feature",
        rendering=_security_rendering,
    ),
    RosterEntry("blitzecdn.capabilities.origins.feature"),
    RosterEntry("blitzecdn.capabilities.security.geoip.feature"),
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
    """Every built-in feature's declarations. Any failure is fatal."""
    return FeatureRegistry(load_features(BUILTIN_FEATURES))


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
