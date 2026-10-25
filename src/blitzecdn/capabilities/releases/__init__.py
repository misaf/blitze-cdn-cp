"""Compiling canonical desired state into an immutable, addressable release.

A release is a pure function of canonical state, the registered capabilities,
and the targeted edges, carrying the digests that make that checkable. It is
the one answer to both "would this converge" and "what did that deployment
send".
"""

from __future__ import annotations

from blitzecdn.capabilities.releases.domain import (
    COMPILER_VERSION,
    DESIRED_STATE_ARTIFACT,
    Artifact,
    CompiledSite,
    HostExplanation,
    Release,
    ReleaseInputs,
    SettingOrigin,
    SettingSource,
)

__all__ = [
    "COMPILER_VERSION",
    "DESIRED_STATE_ARTIFACT",
    "Artifact",
    "CompiledSite",
    "HostExplanation",
    "Release",
    "ReleaseInputs",
    "SettingOrigin",
    "SettingSource",
]
