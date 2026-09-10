"""What compiling a release needs from the world, declared by the compiler.

Two ports and both are functions, because the compiler is a function: what it
needs from an installation is answers, not objects with lifecycles. Declaring
them here rather than importing the plugin registry is what keeps
``compile_release`` testable with two hand-written closures and no plugin
registered anywhere — and what stops the pure core of this capability acquiring
a dependency on the machinery that happens to answer these questions today.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    Release,
    ReleaseFinding,
    ReleaseInputs,
)

__all__ = [
    "CanonicalState",
    "ReleaseReferences",
    "ReleaseStore",
    "SiteChecks",
    "StateContributors",
    "TargetReader",
]


class StateContributors(Protocol):
    """Every installed capability's share of the artifact, already merged.

    Two mappings, not a plugin manager. A capability contributes per-site
    variables — a site's compression settings, its certificate paths — and
    fleet-wide ones, and which capability contributed which is settled before
    the compiler sees any of it.
    """

    def site_variables(self, site: CdnSite) -> Mapping[str, object]: ...

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> Mapping[str, object]: ...


class SiteChecks(Protocol):
    """What the installed capabilities object to about one site.

    Asked once per site while compiling, so refusing costs nothing: no artifact
    is written and no playbook starts. Returns findings rather than raising for
    the reason the whole compiler does — an operator wants every problem, not
    the first one.
    """

    def __call__(self, site: CdnSite) -> Sequence[ReleaseFinding]: ...


class ReleaseStore(Protocol):
    """Where compiled releases and the state they came from are kept.

    Content-addressed: ``save`` is idempotent for a release whose digest is
    already stored, because recompiling unchanged state is the ordinary case —
    every ``blitzecdn validate`` does it — and a history table that grew a row
    each time would be a log of how often somebody asked rather than of what
    the fleet was asked to serve.
    """

    def save(self, release: Release, inputs: ReleaseInputs) -> Release: ...

    def get(self, release_id: str) -> Release: ...

    def inputs_for(self, release_id: str) -> ReleaseInputs: ...

    def list_releases(self, limit: int = 20) -> Sequence[Release]: ...

    def prune(self, keep: int) -> int: ...


class ReleaseReferences(Protocol):
    """Which releases something outside this capability still needs.

    Pruning has to know, and the answer lives in the deployments table: a
    release some deployment converged is the fleet's way back to it, and age
    is no reason to remove one. Declared as a port so this capability never
    reads another's schema to find out — the module that owns those rows
    answers, and the composition root introduces the two.
    """

    def in_use(self) -> Sequence[str]: ...


class CanonicalState(Protocol):
    """The zones, rules and records a compilation reads, as one value.

    One method, because a compilation reads canonical state at one instant or
    it reads an inconsistent mixture of two. Three separate store reads would
    let a record be written between the second and the third, and the release
    would then describe a state that never existed.
    """

    def release_inputs(self) -> ReleaseInputs: ...


class TargetReader(Protocol):
    """The edges a run would reach, and what each of them declares it provides.

    ``host_limit`` is the Ansible ``--limit`` pattern a canary uses. It belongs
    here because it changes the answer: a release aimed at one edge is
    validated against that edge's runtime and not against the rest of a fleet
    it is not going to touch.
    """

    def targets(
        self, *, host_limit: str | None = None
    ) -> Sequence[EdgeCapabilities]: ...
