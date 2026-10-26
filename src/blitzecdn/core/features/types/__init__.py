"""The values features hand back, and the identity they hand back with them.

Frozen dataclasses rather than dictionaries, so a value's shape is a type.
One module per contribution family; import them from `blitzecdn.core.features`.
"""

from __future__ import annotations

from blitzecdn.core.features.types.ansible import AnsibleContribution, EdgeModule
from blitzecdn.core.features.types.configuration import (
    CapabilitySetting,
    ConfigurationContribution,
    ConfigurationModel,
    EnvironmentKey,
    SettingValue,
)
from blitzecdn.core.features.types.feature import (
    Feature,
    FeatureOperations,
    FeatureRendering,
    RequestsCapabilities,
    Site,
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
    "ConfigurationModel",
    "EdgeModule",
    "EnvironmentKey",
    "Feature",
    "FeatureMetadata",
    "FeatureOperations",
    "FeatureRendering",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "RequestsCapabilities",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "Site",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
]
