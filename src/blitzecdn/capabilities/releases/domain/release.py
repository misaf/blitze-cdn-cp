"""The immutable, content-addressed thing a deployment converges.

A release is the whole of one compilation: the state it read, the virtual hosts
that state derives, the document the edges are handed, and the reasons — if any
— that this installation cannot serve it. It is a value. Nothing in it is
filled in later, nothing about it depends on when it was compiled, and its
identity is the digest of its own contents.

That last property is what the rest of the design leans on. Two compilations of
the same canonical state, on installations with the same capabilities, aimed at
the same edges, produce the same digest — so "has anything changed since we
last deployed" is a string comparison rather than a fleet-wide check-mode run,
and "is this release the one that was tested" is answerable after the fact
rather than only while the run is in flight.

Deliberately *not* in a release: a timestamp, an operator, a run identifier, a
status. All four are facts about a deployment of a release, and a release is
converged more than once — a rollback re-converges an old one, a retry
re-converges the one that failed. Putting any of them here would make two
identical configurations two different releases, which is precisely the
comparison this type exists to make cheap.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.releases.domain.explanation import HostExplanation
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

#: Bumped when the compiler would produce a different artifact from identical
#: inputs. It is part of the release digest for exactly that reason: without
#: it, a compiler change would leave yesterday's release looking current, and
#: the fleet would go on serving an artifact no code in the tree still
#: produces. It is not a schema version and it is not a wire version — those
#: belong to the documents, which carry their own.
COMPILER_VERSION = 1

#: The one artifact a release carries today: the document Ansible reads with
#: ``--extra-vars``. A name rather than a position, so a second artifact does
#: not renumber the first and a report can say which digest it is quoting.
#:
#: In the domain and not beside the compiler because a *consumer* needs it: the
#: deployment service asks a release for this artifact by name, and a
#: cross-capability import may reach a capability's values and not its service.
DESIRED_STATE_ARTIFACT = "desired-state"


class UnservableReleaseError(ConfigurationError):
    """Asked for the artifact of a release that was refused.

    A ``ConfigurationError`` rather than a ``KeyError``, because it is one: the
    caller asked for a document that was never rendered, and the reason is a
    configuration an operator has to go and change. The delivery surfaces map
    this to the exit code and the status they map every other configuration
    refusal to — see ``docs/decisions/0006-how-a-failure-reaches-a-caller.md``.
    """


class EdgeCapabilities(BaseModel):
    """One deployment target, and what its runtime is declared to provide.

    A value rather than
    :class:`~blitzecdn.capabilities.edges.domain.edge.Edge`, because the
    compiler needs two fields of an edge and must not be able to reach the rest
    — an edge's SSH user is not an input to a configuration, and a compiler
    that could read it would be a compiler whose output depended on it.

    ``capabilities`` of ``None`` means the edge has declared nothing, and is
    then assumed to provide whatever this installation has installed. That is
    the behaviour every existing installation already has, so an edge registered
    before this field existed keeps converging exactly as it did; declaring the
    set is how an operator opts in to the stricter check.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    capabilities: tuple[str, ...] | None = None


class ReleaseFinding(BaseModel):
    """One reason this release cannot be converged, as an operator reads it.

    Carried on the release rather than raised out of the compiler. Compiling is
    a question, asked by ``blitzecdn validate`` as often as by a deploy, and a
    question that answers by raising cannot report the second problem — which
    is the one an operator meets after fixing the first.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Who found it: a capability token, ``"capabilities"`` for a missing
    #: implementation, ``"derivation"`` for a group that would not compose.
    source: str
    #: The derived host it concerns, or ``None`` for a fleet-wide finding.
    host: str | None
    message: str


class CompiledSite(BaseModel):
    """One virtual host, and the variables the edges are handed for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: CdnSite
    #: What every installed capability contributed for this site, merged. Kept
    #: beside the site rather than folded into it: the site is the stable
    #: schema every wheel binds to, and these are one installation's rendering
    #: of it.
    variables: dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.site.name


class Artifact(BaseModel):
    """A rendered document an edge is converged from, and its digest.

    One artifact today — the desired-state document Ansible reads through
    ``--extra-vars``. Modelled as a list on the release anyway, because the
    thing that makes an artifact an artifact is that it is *rendered from* the
    release rather than *part of* it, and a second one (a per-edge document, an
    exported bundle) must not require the release to change shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    document: dict[str, Any]

    @property
    def digest(self) -> str:
        """The digest of this document's canonical bytes."""
        return _digest(json.dumps(self.document, sort_keys=True, default=str))


class Release(BaseModel):
    """The compiled, addressable answer to "what should the fleet be serving".

    Constructed only by
    :func:`~blitzecdn.capabilities.releases.service.compiler.compile_release`. The
    constructor is not private — a test builds one directly and should be able
    to — but every field is an output of that function, and assembling one by
    hand from something other than its own inputs produces a value whose digest
    describes a compilation that never happened.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    compiler_version: int = COMPILER_VERSION
    #: The digest of the canonical state this was compiled from. The link back
    #: to the inputs, kept as a digest rather than a copy because the inputs
    #: are stored once beside the release and a deployment refers to both.
    inputs_digest: str
    #: The capability tokens installed when this was compiled, sorted. In the
    #: digest because they change the artifact: a site's compression settings
    #: contribute nothing to the document with `blitzecdn-compression` detached.
    capabilities: tuple[str, ...] = ()
    #: The edges this was compiled for, sorted by name. In the digest because
    #: capability validation is against them: the same state can be servable by
    #: one edge and not another.
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
        """This release's identity: the digest of everything that decides it.

        Over the *metadata* — compiler version, inputs digest, capabilities,
        targets — and the artifact digests, rather than over the whole document.
        The sites and the explanations are derived from those inputs by this
        compiler version, so including them would only let a change to the
        explanation format masquerade as a change in what the edges are served.
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
        """The short form of the digest, which is what an operator types.

        Sixteen hex characters. A release is addressed within one installation's
        history, which is bounded by ``history_retention``, so the birthday
        bound on 64 bits is many orders of magnitude clear of anything an
        installation will hold — and the full digest is still on the record for
        anything that needs to be certain.
        """
        return self.digest[:16]

    def artifact(self, name: str) -> Artifact:
        """One artifact by name, refusing rather than returning a default.

        A release an installed capability *objected to* has none at all, and
        the refusal says so with the findings rather than with an empty list of
        names: a capability contributes its variables by being asked, and the
        objection is usually the reason asking would fail.

        A release that is merely unservable — one asking for a capability this
        installation does not have — still has its artifact. The missing wheel
        contributes nothing and the document is the document without it, which
        is what an operator wants to look at while deciding what to install.
        Nothing converges it either way; `servable` is what says so.
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

    def digests(self) -> Mapping[str, str]:
        """Every artifact digest, for a report or a comparison."""
        return {artifact.name: artifact.digest for artifact in self.artifacts}


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
