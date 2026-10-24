"""BlitzeCDN's feature and extension mechanism.

A built-in feature declares a typed :class:`Feature` and has its rendering and
operations built by the composition root. Pluggy is used only for **external
registration**: a third-party distribution telling the control plane that it
exists and what it contributes. Neither is ever used for business
communication. A caller that wants work done calls the service that owns it,
through a constructor-injected reference:

    certificate_service.issue(...)      not   hook.issue_certificate(...)

The difference is that a service call has one implementation, a return value, a
type, and a stack trace that names it. A hook call has none of those, and using
one to ask for a certificate would trade every one of them for indirection
nothing here needs.

A built-in feature module is listed in
:data:`~blitzecdn.composition.BUILTIN_FEATURES` — in the composition root, not
here, because this package is the mechanism and not a participant in it. An
external plugin is a module carrying ``@hookimpl`` functions that advertises
itself in the ``blitzecdn.plugins`` entry-point group and needs no change
anywhere at all.
"""

from blitzecdn.core.plugins.discovery import (
    Discovery,
    PluginRejection,
    load_features,
    register,
    register_external,
)
from blitzecdn.core.plugins.hooks import hookimpl
from blitzecdn.core.plugins.manager import build_plugin_manager, load_plugins
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
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
    PROJECT_NAME,
    SUPPORTED_HOOK_API_VERSIONS,
    AnsibleContribution,
    CapabilitySetting,
    CliCommandGroup,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    ExtensionContext,
    Feature,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    ProcessKind,
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
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "SUPPORTED_HOOK_API_VERSIONS",
    "AnsibleContribution",
    "CapabilityConfig",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "Discovery",
    "EdgeModule",
    "EnvironmentKey",
    "ExtensionContext",
    "Feature",
    "FeatureOperations",
    "FeatureRendering",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "Operations",
    "PluginMetadata",
    "PluginRegistry",
    "PluginRejection",
    "ProcessKind",
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
    "build_plugin_manager",
    "hookimpl",
    "load_features",
    "load_plugins",
    "merge_variables",
    "register",
    "register_external",
    "resolve_capability_environment",
    "resolve_edge_capability_roles",
    "resolve_edge_modules",
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_plugin_configuration",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]
