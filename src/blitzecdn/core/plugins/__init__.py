"""BlitzeCDN's extension mechanism.

Pluggy is used for **registration**: telling the control plane that a feature
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
built-in feature is listed in :data:`~blitzecdn.core.plugins.discovery.BUILTIN_PLUGINS`;
an external distribution advertises itself in the ``blitzecdn.plugins``
entry-point group and needs no change here at all.
"""

from blitzecdn.core.plugins.discovery import (
    BUILTIN_PLUGINS,
    Discovery,
    PluginRejection,
    register,
    register_builtins,
    register_external,
)
from blitzecdn.core.plugins.hooks import hookimpl
from blitzecdn.core.plugins.manager import build_plugin_manager, load_plugins
from blitzecdn.core.plugins.registry import PluginRegistry, merge_variables
from blitzecdn.core.plugins.types import (
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
    PROJECT_NAME,
    AnsibleContribution,
    CliCommandGroup,
    FleetStateContribution,
    HealthCheck,
    PluginMetadata,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    StateValue,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "BUILTIN_PLUGINS",
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "AnsibleContribution",
    "CliCommandGroup",
    "Discovery",
    "FleetStateContribution",
    "HealthCheck",
    "PluginMetadata",
    "PluginRegistry",
    "PluginRejection",
    "ProcessKind",
    "RuntimeContext",
    "ScheduledJob",
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
]
