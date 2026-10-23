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
    """An explicit boundary between resolving resources and compiling a release.

    Canonical state is captured as encoded text because rule overrides contain
    mappings. Holding the original model would let a later edit change the
    input to a compilation already prepared. Plugin results are canonical
    documents for the same reason; no callable or service enters this value.
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
