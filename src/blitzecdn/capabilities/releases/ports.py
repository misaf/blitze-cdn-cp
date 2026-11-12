"""Consumer-owned ports for release preparation, storage and retention.

Live contributors and checks run during preparation, and their answers are
captured as values before the deterministic compiler sees them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    EdgeRelease,
    Release,
    ReleaseFinding,
    ReleaseInputs,
)
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.site import CdnSite

__all__ = [
    "CanonicalState",
    "ConfigurationRenderer",
    "LiveRelease",
    "ReleaseReferences",
    "ReleaseStore",
    "SiteChecks",
    "StateContributors",
    "TargetReader",
]


class ConfigurationRenderer(Protocol):
    """The edge configuration tree a release's execution document renders to."""

    def render(self, document: Mapping[str, Any]) -> ConfigurationBundle:
        """Render an execution document to the configuration tree edges receive."""
        ...


class StateContributors(Protocol):
    """Every capability's per-site and fleet variables, already merged."""

    def site_variables(self, site: CdnSite) -> Mapping[str, object]:
        """Every capability's variables for one site, merged into one mapping."""
        ...

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> Mapping[str, object]:
        """Every capability's fleet-wide variables for ``sites``, merged."""
        ...


class SiteChecks(Protocol):
    """What the capabilities object to about one site, as findings."""

    def __call__(self, site: CdnSite) -> Sequence[ReleaseFinding]: ...


class ReleaseStore(Protocol):
    """Where compiled releases and their inputs are kept; ``save`` is idempotent."""

    def save(self, release: Release, inputs: ReleaseInputs) -> Release:
        """Put a release and the inputs it was compiled from on record.

        Saving one already stored leaves it as it is, since concurrent compiles of
        unchanged state are ordinary. Raises ``ConfigurationError`` if ``inputs`` is
        not what ``release`` was compiled from.
        """
        ...

    def save_configuration(self, release: Release, bundle: ConfigurationBundle) -> None:
        """Record what ``release`` rendered to, once; a later call changes nothing.

        Rollbacks ship that first render.
        """
        ...

    def configuration(self, release_id: str) -> ConfigurationBundle:
        """The configuration a stored release rendered to, verified.

        Raises ``NotFoundError`` if none is on record and ``ConfigurationError`` if
        it fails its integrity check.
        """
        ...

    def get(self, release_id: str) -> Release:
        """One stored release; raises ``NotFoundError`` if there is none by that id."""
        ...

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a stored release was compiled from, verified.

        Raises ``NotFoundError`` for an unknown release and ``ConfigurationError``
        for inputs that fail their integrity check.
        """
        ...

    def paginate_releases(self, *, limit: int, offset: int) -> PageResult[Release]:
        """One page of stored releases, newest first."""
        ...

    def list_releases(self, limit: int | None = 20) -> Sequence[Release]:
        """The ``limit`` most recent releases, newest first; ``None`` for all."""
        ...

    def prune(self, keep: int) -> int:
        """Drop releases beyond the newest ``keep`` that no deployment names.

        Returns how many were removed.
        """
        ...


class LiveRelease(Protocol):
    """Which releases the fleet is running, as deployment history records it.

    Records of what deployments did, not probes of what the edges serve now.
    """

    def live_release(self) -> str | None:
        """The release of the newest real run that reached any edge, or ``None``."""
        ...

    def edge_releases(self) -> Mapping[str, EdgeRelease]:
        """What history records for every edge a real run ever touched, by name.

        An edge's release is the one its newest verified run applied; a newer real
        run that failed or stopped on it after activating makes it ``UNVERIFIED``.
        Edges no real run ever verified are ``UNKNOWN`` or absent.
        """
        ...


class ReleaseReferences(Protocol):
    """Which releases something outside this capability still needs."""

    def in_use(self) -> Sequence[str]:
        """Every release id still named elsewhere; pruning must keep them."""
        ...


class CanonicalState(Protocol):
    """The zones, rules and records a compilation reads, at one instant.

    One method, so a write between separate reads cannot yield a state that never
    existed.
    """

    def release_inputs(self) -> ReleaseInputs:
        """Zones, rules, records and settings, read together in one snapshot."""
        ...


class TargetReader(Protocol):
    """The edges a run would reach, and what each declares it provides.

    ``host_limit`` narrows validation to the edges a canary touches.
    """

    def targets(self, *, host_limit: str | None = None) -> Sequence[EdgeCapabilities]:
        """The edges ``host_limit`` selects, or all, with what each declares."""
        ...
