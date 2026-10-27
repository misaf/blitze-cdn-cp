"""Consumer-owned ports for release preparation, storage and retention.

Live contributors and checks run during preparation, and their answers are
captured as values before the deterministic compiler sees them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    Release,
    ReleaseFinding,
    ReleaseInputs,
)
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.pagination import PageResult

__all__ = [
    "CanonicalState",
    "ConfigurationRenderer",
    "ReleaseReferences",
    "ReleaseStore",
    "SiteChecks",
    "StateContributors",
    "TargetReader",
]


class ConfigurationRenderer(Protocol):
    """The edge configuration tree a release's execution document renders to."""

    def render(self, document: Mapping[str, Any]) -> ConfigurationBundle: ...


class StateContributors(Protocol):
    """Every capability's per-site and fleet variables, already merged."""

    def site_variables(self, site: CdnSite) -> Mapping[str, object]: ...

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> Mapping[str, object]: ...


class SiteChecks(Protocol):
    """What the capabilities object to about one site, as findings."""

    def __call__(self, site: CdnSite) -> Sequence[ReleaseFinding]: ...


class ReleaseStore(Protocol):
    """Where compiled releases and their inputs are kept; ``save`` is idempotent."""

    def save(self, release: Release, inputs: ReleaseInputs) -> Release: ...

    def save_configuration(
        self, release: Release, bundle: ConfigurationBundle
    ) -> None: ...

    def configuration(self, release_id: str) -> ConfigurationBundle: ...

    def get(self, release_id: str) -> Release: ...

    def inputs_for(self, release_id: str) -> ReleaseInputs: ...

    def paginate_releases(self, *, limit: int, offset: int) -> PageResult[Release]: ...

    def list_releases(self, limit: int | None = 20) -> Sequence[Release]: ...

    def prune(self, keep: int) -> int: ...


class ReleaseReferences(Protocol):
    """Which releases something outside this capability still needs."""

    def in_use(self) -> Sequence[str]: ...


class CanonicalState(Protocol):
    """The zones, rules and records a compilation reads, at one instant.

    One method, so a write between separate reads cannot yield a state that never
    existed.
    """

    def release_inputs(self) -> ReleaseInputs: ...


class TargetReader(Protocol):
    """The edges a run would reach, and what each declares it provides.

    ``host_limit`` narrows validation to the edges a canary touches.
    """

    def targets(
        self, *, host_limit: str | None = None
    ) -> Sequence[EdgeCapabilities]: ...
