"""The captured values a compiler may read, with no live collaborators."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.releases.domain.inputs import ReleaseInputs
from blitzecdn.capabilities.releases.domain.release import (
    Artifact,
    CompiledSite,
    EdgeCapabilities,
    ReleaseFinding,
)


class Compilation(BaseModel):
    """The boundary between resolving resources and compiling a release.

    Canonical state is captured as encoded text and feature results as canonical
    documents, so a later edit cannot change a prepared compilation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_inputs: str
    capabilities: tuple[str, ...]
    targets: tuple[EdgeCapabilities, ...]
    sites: tuple[CompiledSite, ...]
    fleet_variables: Artifact
    objections: tuple[ReleaseFinding, ...]
    allow_empty_sites: bool

    @property
    def inputs(self) -> ReleaseInputs:
        return ReleaseInputs.decode(self.canonical_inputs)
