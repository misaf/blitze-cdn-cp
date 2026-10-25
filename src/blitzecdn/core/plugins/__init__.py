"""BlitzeCDN's feature mechanism.

A feature declares a typed :class:`Feature` and has its rendering and
operations bound by the composition root. Nothing here is used for business
communication. A caller that wants work done calls the service that owns it,
through a constructor-injected reference:

    certificate_service.issue(...)      not   hook.issue_certificate(...)

The difference is that a service call has one implementation, a return value, a
type, and a stack trace that names it.

Feature modules are listed in :data:`~blitzecdn.composition.ROSTER` — in the
composition root, not here, because this package is the mechanism and not a
participant in it.
"""

from blitzecdn.core.plugins.discovery import load_features
from blitzecdn.core.plugins.registry import (
    Operations,
    PluginRegistry,
    Rendering,
    merge_variables,
)
from blitzecdn.core.plugins.resolution import (
    CapabilityConfig,
    ResolvedCapabilityEnvironment,
    ResolvedEdgeModule,
    ResolvedNginxResource,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_plugin_configuration,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.plugins.types import (
    AnsibleContribution,
    CapabilitySetting,
    CliCommandGroup,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    Feature,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    ScheduledJob,
    SettingValue,
    Severity,
    SiteReader,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "AnsibleContribution",
    "CapabilityConfig",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "EdgeModule",
    "EnvironmentKey",
    "Feature",
    "FeatureOperations",
    "FeatureRendering",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "Operations",
    "PluginMetadata",
    "PluginRegistry",
    "Rendering",
    "ResolvedCapabilityEnvironment",
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "SiteReader",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
    "load_features",
    "merge_variables",
    "resolve_capability_environment",
    "resolve_edge_capability_roles",
    "resolve_edge_modules",
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_plugin_configuration",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]
