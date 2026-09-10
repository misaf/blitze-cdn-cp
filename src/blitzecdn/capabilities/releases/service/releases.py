"""Compiling the current desired state, and keeping what was compiled.

The service is thin on purpose: the decisions are all in
:func:`~blitzecdn.capabilities.releases.service.compiler.compile_release`, which
is a pure function, and everything here is the I/O that function refuses to do
— reading canonical state, reading which edges exist, and putting the result on
record.

That split is the point of the capability. "What would this state compile to"
is answerable without a database because the answer does not need one; "what
did we compile, and when" needs one and gets exactly one.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    Release,
    ReleaseFinding,
    ReleaseInputs,
)
from blitzecdn.capabilities.releases.ports import (
    CanonicalState,
    ReleaseStore,
    SiteChecks,
    StateContributors,
    TargetReader,
)
from blitzecdn.capabilities.releases.service.compiler import compile_release
from blitzecdn.core.ports import UnitOfWork

__all__ = ["ReleaseService"]


class ReleaseService:
    """Compile the current desired state into a stored, addressable release."""

    def __init__(
        self,
        *,
        state: CanonicalState,
        targets: TargetReader,
        releases: ReleaseStore,
        contributors: StateContributors,
        site_checks: SiteChecks,
        capabilities: Sequence[str],
        allow_empty_sites: bool,
        uow: UnitOfWork,
        retention: int,
    ) -> None:
        self.state = state
        self.targets = targets
        self.releases = releases
        self.contributors = contributors
        self.site_checks = site_checks
        #: What is installed beside this control plane. A sequence captured at
        #: composition rather than a registry: which wheels are installed
        #: cannot change inside a process, and a service that could ask again
        #: would invite a compilation whose inputs moved underneath it.
        self.capabilities = tuple(capabilities)
        self.allow_empty_sites = allow_empty_sites
        self.uow = uow
        self.retention = retention

    def inputs(self) -> ReleaseInputs:
        """Canonical desired state as it stands right now."""
        return self.state.release_inputs()

    def compile(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state without recording anything.

        What ``blitzecdn validate`` asks. Nothing is written, so asking is free
        and asking twice gives the same answer — which is what lets an operator
        check a fix without adding a row to the history of what the fleet was
        asked to serve.
        """
        return compile_release(
            self.inputs(),
            capabilities=self.capabilities,
            targets=self.targets.targets(host_limit=host_limit),
            contributors=self.contributors,
            site_checks=self.site_checks,
            allow_empty_sites=self.allow_empty_sites,
        )

    def prepare(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state and put it on record.

        The step a deployment takes before it converges anything. Recording is
        idempotent by digest, so preparing unchanged state twice yields one row
        and the same identifier — and a deployment that names that identifier
        is naming a document nothing can subsequently edit.
        """
        inputs = self.inputs()
        release = compile_release(
            inputs,
            capabilities=self.capabilities,
            targets=self.targets.targets(host_limit=host_limit),
            contributors=self.contributors,
            site_checks=self.site_checks,
            allow_empty_sites=self.allow_empty_sites,
        )
        with self.uow.transaction():
            stored = self.releases.save(release, inputs)
            self.releases.prune(self.retention)
        return stored

    def get(self, release_id: str) -> Release:
        return self.releases.get(release_id)

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a stored release was compiled from."""
        return self.releases.inputs_for(release_id)

    def list_releases(self, limit: int = 20) -> Sequence[Release]:
        return self.releases.list_releases(limit)

    def findings(self, *, host_limit: str | None = None) -> tuple[ReleaseFinding, ...]:
        """Every reason the current state cannot be converged."""
        return self.compile(host_limit=host_limit).findings

    def sites(self) -> tuple[CdnSite, ...]:
        """The virtual hosts the current state compiles to."""
        return tuple(entry.site for entry in self.compile().sites)

    def edge_capabilities(
        self, *, host_limit: str | None = None
    ) -> tuple[EdgeCapabilities, ...]:
        """The edges a run would be aimed at, and what each declares."""
        return tuple(self.targets.targets(host_limit=host_limit))
