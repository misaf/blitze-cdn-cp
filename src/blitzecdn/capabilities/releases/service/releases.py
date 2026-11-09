"""Compiling the current desired state, and keeping what was compiled.

Thin by design: the decisions are in the pure
:func:`~blitzecdn.capabilities.releases.service.compiler.compile_release`;
this does the I/O it refuses to do.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    Release,
    ReleaseFinding,
    ReleaseInputs,
)
from blitzecdn.capabilities.releases.domain.compilation import Compilation
from blitzecdn.capabilities.releases.ports import (
    CanonicalState,
    ConfigurationRenderer,
    ReleaseStore,
    SiteChecks,
    StateContributors,
    TargetReader,
)
from blitzecdn.capabilities.releases.service.compiler import compile_release
from blitzecdn.capabilities.releases.service.preparation import prepare_compilation
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import NotFoundError
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
        renderer: ConfigurationRenderer,
    ) -> None:
        self.state = state
        self.targets = targets
        self.releases = releases
        self.contributors = contributors
        self.site_checks = site_checks
        #: The shipped features, captured once; they cannot change within a process.
        self.capabilities = tuple(capabilities)
        self.allow_empty_sites = allow_empty_sites
        self.uow = uow
        self.retention = retention
        self.renderer = renderer

    def inputs(self) -> ReleaseInputs:
        """Canonical desired state as it stands right now."""
        return self.state.release_inputs()

    def compile(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state without recording anything."""
        return compile_release(self.capture(host_limit=host_limit))

    def capture(self, *, host_limit: str | None = None) -> Compilation:
        """Snapshot facts, then resolve local resources within any outer transaction."""
        with self.uow.transaction():
            inputs = self.inputs()
            targets = self.targets.targets(host_limit=host_limit)
        return prepare_compilation(
            inputs,
            capabilities=self.capabilities,
            targets=targets,
            contributors=self.contributors,
            site_checks=self.site_checks,
            allow_empty_sites=self.allow_empty_sites,
        )

    def prepare(self, *, host_limit: str | None = None) -> Release:
        """Compile the current state and put it on record, idempotently by digest."""
        # Retain local material before collection can run; render after commit.
        with self.uow.transaction():
            captured = self.capture(host_limit=host_limit)
            inputs = captured.inputs
            release = compile_release(captured)
            stored = self.releases.save(release, inputs)
            self.releases.prune(self.retention)
        if stored.artifacts:
            self._record_configuration(stored)
        return stored

    def _record_configuration(self, release: Release) -> None:
        """Render a prepared release once and keep what it rendered to.

        Rendered outside the write transaction (it takes seconds) and only when
        nothing is recorded. If interrupted, the next preparation fills it in, and a
        deployment refuses to ship a release without one.
        """
        try:
            self.releases.configuration(release.id)
        except NotFoundError:
            bundle = self.renderer.render(release.execution_document())
            with self.uow.transaction():
                self.releases.save_configuration(release, bundle)

    def configuration(self, release_id: str) -> ConfigurationBundle:
        """The configuration a prepared release rendered to, as it was rendered."""
        return self.releases.configuration(release_id)

    def render(self, release: Release) -> ConfigurationBundle:
        """The configuration tree this release renders to, recorded if prepared."""
        try:
            return self.releases.configuration(release.id)
        except NotFoundError:
            return self.renderer.render(release.execution_document())

    def get(self, release_id: str) -> Release:
        return self.releases.get(release_id)

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a stored release was compiled from."""
        return self.releases.inputs_for(release_id)

    def paginate_releases(self, *, limit: int, offset: int) -> PageResult[Release]:
        return self.releases.paginate_releases(limit=limit, offset=offset)

    def list_releases(self, limit: int | None = 20) -> Sequence[Release]:
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
