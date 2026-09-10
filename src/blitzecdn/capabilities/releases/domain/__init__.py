"""The values a compilation produces, and the state it reads."""

from __future__ import annotations

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
    "INPUTS_SCHEMA_VERSION",
    "Artifact",
    "CompiledSite",
    "EdgeCapabilities",
    "HostExplanation",
    "Release",
    "ReleaseFinding",
    "ReleaseInputs",
    "SettingOrigin",
    "SettingSource",
    "UnservableReleaseError",
    "explain",
]
