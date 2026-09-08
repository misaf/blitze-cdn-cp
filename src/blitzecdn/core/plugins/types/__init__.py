"""The values plugins hand back, and the identity they hand back with them.

Everything here is a frozen dataclass rather than a dictionary. A contribution
crosses a package boundary — often one shipped as a separate distribution — so
"which keys does this have" has to be answerable by reading a type rather than
by reading whichever plugin happened to produce the value.

Nothing in this module imports a capability. `core` is what a capability builds on;
the two runtime-bound hooks that need built services take the composition root
as an argument instead, which keeps the arrow pointing one way.

One module per hook family, and this façade over all of them. The split is by
*which hook answers with it*, so a change to what a capability may put on an
edge host touches `ansible` and nothing beside it. Import from
`blitzecdn.core.plugins` — the submodules here publish nothing that façade does
not, which `tests/published_surface.py` derives rather than asserts.
"""

from __future__ import annotations

from blitzecdn.core.plugins.types.ansible import AnsibleContribution, EdgeModule
from blitzecdn.core.plugins.types.configuration import (
    CapabilitySetting,
    ConfigurationContribution,
    EnvironmentKey,
    SettingValue,
)
from blitzecdn.core.plugins.types.identity import (
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
    PROJECT_NAME,
    SUPPORTED_HOOK_API_VERSIONS,
    PluginMetadata,
)
from blitzecdn.core.plugins.types.nginx import NginxContribution
from blitzecdn.core.plugins.types.runtime import (
    CliCommandGroup,
    HealthCheck,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
)
from blitzecdn.core.plugins.types.state import (
    FleetStateContribution,
    SiteStateContribution,
    StateValue,
)
from blitzecdn.core.plugins.types.validation import (
    Severity,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "SUPPORTED_HOOK_API_VERSIONS",
    "AnsibleContribution",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "EdgeModule",
    "EnvironmentKey",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "PluginMetadata",
    "ProcessKind",
    "RuntimeContext",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
]
