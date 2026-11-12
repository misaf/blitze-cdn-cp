"""The values a compilation produces, and the state it reads."""

from __future__ import annotations

from blitzecdn.capabilities.releases.domain.diff import (
    FLEET_OBSERVATION_SOURCE,
    EdgeRelease,
    EdgeReleaseState,
    FleetReleases,
    ReleaseDiff,
    ReleaseGroup,
)
from blitzecdn.capabilities.releases.domain.explanation import (
    HostExplanation,
    SettingOrigin,
    SettingSource,
    explain,
)
from blitzecdn.capabilities.releases.domain.inputs import (
    INPUTS_SCHEMA_VERSION,
    ReleaseInputs,
)
from blitzecdn.capabilities.releases.domain.release import (
    COMPILER_VERSION,
    DESIRED_STATE_ARTIFACT,
    Artifact,
    CompiledSite,
    EdgeCapabilities,
    Release,
    ReleaseFinding,
    UnservableReleaseError,
)

__all__ = [
    "COMPILER_VERSION",
    "DESIRED_STATE_ARTIFACT",
    "FLEET_OBSERVATION_SOURCE",
    "INPUTS_SCHEMA_VERSION",
    "Artifact",
    "CompiledSite",
    "EdgeCapabilities",
    "EdgeRelease",
    "EdgeReleaseState",
    "FleetReleases",
    "HostExplanation",
    "Release",
    "ReleaseDiff",
    "ReleaseFinding",
    "ReleaseGroup",
    "ReleaseInputs",
    "SettingOrigin",
    "SettingSource",
    "UnservableReleaseError",
    "explain",
]
