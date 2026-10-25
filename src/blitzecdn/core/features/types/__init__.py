"""The values features hand back, and the identity they hand back with them.

Everything here is a frozen dataclass rather than a dictionary, so "which keys
does this have" is answerable by reading a type rather than by reading
whichever feature happened to produce the value.

Nothing in this module imports a capability. `core` is what a capability builds
on; behaviour that needs collaborators is bound by the composition root through
`FeatureRendering` and `FeatureOperations`, which keeps the arrow pointing one way.

One module per contribution family, and this façade over all of them, so a
change to what a capability may put on an edge host touches `ansible` and
nothing beside it. Import from
`blitzecdn.core.features` — the submodules here publish nothing that façade does
not, which `tests/published_surface.py` derives rather than asserts.
"""

from __future__ import annotations

from blitzecdn.core.features.types.ansible import AnsibleContribution, EdgeModule
from blitzecdn.core.features.types.configuration import (
    CapabilitySetting,
    ConfigurationContribution,
    EnvironmentKey,
    SettingValue,
)
from blitzecdn.core.features.types.feature import (
    Feature,
    FeatureOperations,
    FeatureRendering,
    SiteReader,
)
from blitzecdn.core.features.types.identity import FeatureMetadata
from blitzecdn.core.features.types.nginx import NginxContribution
from blitzecdn.core.features.types.runtime import (
    CliCommandGroup,
    HealthCheck,
    ScheduledJob,
)
from blitzecdn.core.features.types.state import (
    FleetStateContribution,
    SiteStateContribution,
    StateValue,
)
from blitzecdn.core.features.types.validation import (
    Severity,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "AnsibleContribution",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "EdgeModule",
    "EnvironmentKey",
    "Feature",
    "FeatureMetadata",
    "FeatureOperations",
    "FeatureRendering",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "SiteReader",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
]
