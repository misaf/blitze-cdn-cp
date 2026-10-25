"""The application's features, and the behaviour each one binds.

One row per feature module. The module is named by path because its
`feature.py` imports routers and command groups, and those import this
package: importing feature modules here would be a cycle. A row's builders
bind the feature's rendering and operations from the composition's input
records, calling the capability's own `runtime.py` builders with named
arguments, so a capability never sees either record.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from blitzecdn.capabilities.deployments.runtime import deployments_operations
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.diagnostics.runtime import diagnostics_operations
from blitzecdn.capabilities.dns.runtime import RENDERING as DNS_RENDERING
from blitzecdn.capabilities.http.runtime import RENDERING as HTTP_RENDERING
from blitzecdn.capabilities.security.config import SecurityConfig
from blitzecdn.capabilities.security.runtime import security_rendering
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.capabilities.tls.runtime import (
    certificates_operations,
    certificates_rendering,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRegistry,
    FeatureRendering,
    load_features,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.providers.http3.runtime import RENDERING as HTTP3_RENDERING

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


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One feature module and the behaviour it binds, if any."""

    module: str
    rendering: Callable[[RenderingInputs], FeatureRendering] | None = None
    operations: Callable[[OperationInputs], FeatureOperations] | None = None


def _security_rendering(inputs: RenderingInputs) -> FeatureRendering:
    return security_rendering(
        config=SecurityConfig.from_capability_config(
            inputs.capability_config.for_feature("security")
        )
    )


def _certificates_rendering(inputs: RenderingInputs) -> FeatureRendering:
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
    RosterEntry("blitzecdn.capabilities.compression.feature"),
    RosterEntry(
        "blitzecdn.capabilities.security.feature",
        rendering=_security_rendering,
    ),
    RosterEntry("blitzecdn.capabilities.origins.feature"),
    RosterEntry("blitzecdn.providers.geoip.feature"),
    RosterEntry("blitzecdn.providers.hardening.feature"),
    RosterEntry(
        "blitzecdn.providers.http3.feature",
        rendering=lambda _inputs: HTTP3_RENDERING,
    ),
    RosterEntry("blitzecdn.providers.resolver.feature"),
)

#: The feature modules alone, in roster order.
BUILTIN_FEATURES: tuple[str, ...] = tuple(entry.module for entry in ROSTER)


def load_registry() -> FeatureRegistry:
    """Every built-in feature's declarations. Any failure is fatal."""
    return FeatureRegistry(load_features(BUILTIN_FEATURES))


def bind_renderings(inputs: RenderingInputs) -> tuple[FeatureRendering, ...]:
    """Each row's rendering, in roster order."""
    return tuple(
        entry.rendering(inputs) for entry in ROSTER if entry.rendering is not None
    )


def bind_operations(inputs: OperationInputs) -> tuple[FeatureOperations, ...]:
    """Each row's operations, in roster order."""
    return tuple(
        entry.operations(inputs) for entry in ROSTER if entry.operations is not None
    )
