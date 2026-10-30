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

from blitzecdn.core.features.discovery import load_features
from blitzecdn.core.features.registry import (
    FeatureRegistry,
    Operations,
    Rendering,
    merge_variables,
)
from blitzecdn.core.features.resolution import (
    CapabilityConfig,
    ResolvedCapabilityEnvironment,
    ResolvedEdgeModule,
    ResolvedNginxResource,
    ResolvedPolicySchema,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_feature_configuration,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_policy_schema,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.features.types import (
    AnsibleContribution,
    CapabilitySetting,
    CliCommandGroup,
    ConfigurationContribution,
    ConfigurationModel,
    EdgeModule,
    EnvironmentKey,
    Feature,
    FeatureMetadata,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PolicyCheck,
    PolicyContribution,
    PolicyView,
    RequestsCapabilities,
    ScheduledJob,
    SettingValue,
    Severity,
    Site,
    SiteCheck,
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
    "ConfigurationModel",
    "EdgeModule",
    "EnvironmentKey",
    "Feature",
    "FeatureMetadata",
    "FeatureOperations",
    "FeatureRegistry",
    "FeatureRendering",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "Operations",
    "PolicyCheck",
    "PolicyContribution",
    "PolicyView",
    "Rendering",
    "RequestsCapabilities",
    "ResolvedCapabilityEnvironment",
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "ResolvedPolicySchema",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "Site",
    "SiteCheck",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
    "load_features",
    "merge_variables",
    "resolve_capability_environment",
    "resolve_edge_capability_roles",
    "resolve_edge_modules",
    "resolve_feature_configuration",
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_policy_schema",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]
