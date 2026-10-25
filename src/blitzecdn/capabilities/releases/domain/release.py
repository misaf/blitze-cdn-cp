"""The immutable, content-addressed thing a deployment converges.

The state read, the hosts derived, the document handed to edges, and any
reasons this installation cannot serve it. Its identity is the digest of its
contents, so "has anything changed since the last deploy" is a string
comparison. No timestamp, operator, run id or status: those belong to a
deployment, and a release is converged more than once.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator

from blitzecdn.capabilities.releases.domain.explanation import HostExplanation
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.domain.documents import canonical_document, read_document
from blitzecdn.core.exceptions import ConfigurationError

__all__ = [
    "COMPILER_VERSION",
    "DESIRED_STATE_ARTIFACT",
    "Artifact",
    "CompiledSite",
    "EdgeCapabilities",
    "Release",
    "ReleaseFinding",
    "UnservableReleaseError",
]

#: Bumped when identical inputs would compile to a different artifact; part of
#: the digest so a compiler change never leaves an old release looking current.
COMPILER_VERSION = 4

#: The one artifact today, by name. In the domain because the deployment
#: service asks for it across capabilities.
DESIRED_STATE_ARTIFACT = "desired-state"


class UnservableReleaseError(ConfigurationError):
    """Asked for the artifact of a refused release; a configuration error."""


class EdgeCapabilities(BaseModel):
    """One deployment target, and what its runtime is declared to provide.

    Two fields, not an ``Edge``, so the compiler cannot depend on the rest.
    ``capabilities`` of ``None`` means undeclared, assumed to provide everything
    installed; declaring a set opts in to the stricter check.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    capabilities: tuple[str, ...] | None = None


class ReleaseFinding(BaseModel):
    """One reason this release cannot be converged, carried rather than raised.

    So ``validate`` can report every problem at once.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Who found it: a capability token, ``"capabilities"``, ``"derivation"`` or
    #: ``"settings"``.
    source: str
    #: The derived host it concerns, or ``None`` for a fleet-wide finding.
    host: str | None
    message: str


class CompiledSite(BaseModel):
    """One virtual host, and the variables the edges are handed for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: CdnSite
    #: What every capability contributed for this site, merged, beside the site.
    variables_json: str = "{}"

    @classmethod
    def from_variables(cls, site: CdnSite, variables: Mapping[str, object]) -> Self:
        return cls(site=site, variables_json=canonical_document(dict(variables)))

    @field_validator("variables_json", mode="before")
    @classmethod
    def canonicalize_variables(cls, value: object) -> str:
        return canonical_document(value)

    @property
    def variables(self) -> dict[str, Any]:
        """A working copy; the canonical document remains unchanged."""
        return read_document(self.variables_json)

    @property
    def name(self) -> str:
        return self.site.name


class Artifact(BaseModel):
    """A rendered document an edge is converged from, and its digest.

    One today, the desired-state document; a list so a second needs no reshape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    content: str

    @classmethod
    def from_document(cls, name: str, document: Mapping[str, object]) -> Self:
        return cls(name=name, content=canonical_document(dict(document)))

    @field_validator("content", mode="before")
    @classmethod
    def canonicalize_content(cls, value: object) -> str:
        return canonical_document(value)

    @property
    def document(self) -> dict[str, Any]:
        """Decode a fresh view instead of exposing mutable identity data."""
        return read_document(self.content)

    @property
    def digest(self) -> str:
        """The digest of this document's canonical bytes."""
        return _digest(self.content)


class Release(BaseModel):
    """The compiled, addressable answer to "what should the fleet be serving".

    Built by :func:`~blitzecdn.capabilities.releases.service.compiler.compile_release`;
    a hand-assembled one has a digest describing no real compilation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    compiler_version: int = COMPILER_VERSION
    #: The digest of the canonical inputs, stored once beside the release.
    inputs_digest: str
    #: Assembled capability tokens when this was compiled, sorted. They form
    #: part of release identity because runtime contributions affect artifacts.
    capabilities: tuple[str, ...] = ()
    #: The targeted edges, sorted; in the digest because servability depends on them.
    targets: tuple[EdgeCapabilities, ...] = ()
    sites: tuple[CompiledSite, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    explanations: tuple[HostExplanation, ...] = ()
    findings: tuple[ReleaseFinding, ...] = ()

    @property
    def servable(self) -> bool:
        """Whether anything found here should stop this release converging."""
        return not self.findings

    @property
    def digest(self) -> str:
        """This release's identity, over its metadata and artifact digests.

        Sites and explanations are derived from those, so including them would let an
        explanation format change pass for a change in what edges serve.
        """
        return _digest(
            json.dumps(
                {
                    "compiler_version": self.compiler_version,
                    "inputs_digest": self.inputs_digest,
                    "capabilities": list(self.capabilities),
                    "targets": [
                        target.model_dump(mode="json") for target in self.targets
                    ],
                    "artifacts": {
                        artifact.name: artifact.digest for artifact in self.artifacts
                    },
                },
                sort_keys=True,
            )
        )

    @property
    def id(self) -> str:
        """The 16-hex-character short digest, ample within retained history."""
        return self.digest[:16]

    def artifact(self, name: str) -> Artifact:
        """One artifact by name, refusing rather than defaulting.

        A release a capability objected to has none. One merely unservable — missing a
        capability — still has its document, for inspection; ``servable`` says nothing
        converges it.
        """
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        if self.findings:
            reasons = "; ".join(finding.message for finding in self.findings)
            raise UnservableReleaseError(
                f"release {self.id} was not compiled to artifacts because it "
                f"cannot be converged: {reasons}"
            )
        available = ", ".join(sorted(item.name for item in self.artifacts)) or "none"
        raise KeyError(f"release {self.id} has no {name!r} artifact; has: {available}")

    def execution_document(self) -> dict[str, Any]:
        """The artifact plus its identity, avoiding a self-referential hash."""
        return {
            **self.artifact(DESIRED_STATE_ARTIFACT).document,
            "blitzecdn_release_digest": self.digest,
        }

    def digests(self) -> Mapping[str, str]:
        """Every artifact digest, for a report or a comparison."""
        return {artifact.name: artifact.digest for artifact in self.artifacts}


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
