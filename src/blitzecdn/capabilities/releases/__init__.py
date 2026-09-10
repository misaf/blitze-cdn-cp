"""Compiling canonical desired state into an immutable, addressable release.

A *release* is what a deployment converges. It is produced by a pure function
of canonical state — zones, their rules, their records — and of the two things
outside that state which change what the edges are asked to do: the
capabilities installed beside this control plane, and the edges the run is
aimed at.

The capability exists because those two questions used to be answered in three
different places at three different times. Desired state was serialised at
queue time, rendered to YAML under the deployment lock moments before Ansible
read it, and validated against the installed plugins somewhere in between — so
"what did that deployment actually send" had no answer after the fact, and
"would this state converge" and "did this state converge" were computed by
different code from different inputs.

Now there is one answer and it is a value: :class:`~.domain.Release`. Compiling
it touches no database and no network, it is a function of its inputs alone,
and it carries the digests that make that claim checkable.
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
