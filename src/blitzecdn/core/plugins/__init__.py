"""BlitzeCDN's extension mechanism.

Pluggy is used for **registration**: telling the control plane that a capability
exists and what it contributes — routes, commands, jobs, health checks, desired
state, deployment checks, lifecycle work. It is never used for business
communication. A caller that wants work done calls the service that owns it,
through a constructor-injected reference:

    certificate_service.issue(...)      not   hook.issue_certificate(...)

The difference is that a service call has one implementation, a return value, a
type, and a stack trace that names it. A hook call has none of those, and using
one to ask for a certificate would trade every one of them for indirection
nothing here needs.

A plugin is a module (or any object) carrying ``@hookimpl`` functions. A
built-in capability is listed in
:data:`~blitzecdn.composition.BUILTIN_PLUGINS` — in the composition root, not
here, because this package is the mechanism and not a participant in it; an
external distribution advertises itself in the ``blitzecdn.plugins``
entry-point group and needs no change anywhere at all.
"""

from blitzecdn.core.plugins.discovery import (
    Discovery,
    PluginRejection,
    register,
    register_builtins,
    register_external,
)
from blitzecdn.core.plugins.hooks import hookimpl
from blitzecdn.core.plugins.manager import build_plugin_manager, load_plugins
from blitzecdn.core.plugins.registry import PluginRegistry, merge_variables
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
    AnsibleContribution,
    CapabilitySetting,
    CliCommandGroup,
    ConfigurationContribution,
    EdgeModule,
    EnvironmentKey,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
    SettingValue,
    Severity,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "AnsibleContribution",
    "CapabilityConfig",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "Discovery",
    "EdgeModule",
    "EnvironmentKey",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "PluginMetadata",
    "PluginRegistry",
    "PluginRejection",
    "ProcessKind",
    "ResolvedCapabilityEnvironment",
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "RuntimeContext",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
    "build_plugin_manager",
    "hookimpl",
    "load_plugins",
    "merge_variables",
    "register",
    "register_builtins",
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
