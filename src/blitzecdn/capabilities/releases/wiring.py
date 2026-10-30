"""How the releases capability is built: settling the pure compiler's inputs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.capabilities.releases.domain import EdgeCapabilities, ReleaseFinding
from blitzecdn.capabilities.releases.ports import (
    CanonicalState,
    ConfigurationRenderer,
    ReleaseStore,
    StateContributors,
)
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.validation import matches_edge_limit
from blitzecdn.core.features import Severity, ValidationIssue
from blitzecdn.core.ports import UnitOfWork

__all__ = ["FeatureSiteChecks", "StoredEdgeTargets", "build_release_service"]

_LOGGER = logging.getLogger(__name__)


class FeatureSiteChecks:
    """The compiler's view of what the capabilities object to.

    Logs warnings here, since the pure compiler has no logger, and turns only
    blocking issues into findings. Uses ``site_objections``: the compiler checks
    missing capabilities itself, against edges too. Messages name the objecting
    feature.
    """

    def __init__(
        self, objections: Callable[[CdnSite], Sequence[ValidationIssue]]
    ) -> None:
        self._objections = objections

    def __call__(self, site: CdnSite) -> Sequence[ReleaseFinding]:
        findings: list[ReleaseFinding] = []
        for issue in self._objections(site):
            message = f"{issue.feature}: {issue.site}: {issue.message}"
            if issue.severity is Severity.BLOCKING:
                findings.append(
                    ReleaseFinding(
                        source=issue.feature, host=site.name, message=message
                    )
                )
            else:
                _LOGGER.warning("%s", message)
        return findings


class StoredEdgeTargets:
    """The registered fleet, narrowed by ``host_limit`` as the runner does."""

    def __init__(self, edges: EdgeStore) -> None:
        self._edges = edges

    def targets(self, *, host_limit: str | None = None) -> Sequence[EdgeCapabilities]:
        return [
            EdgeCapabilities(name=edge.name, capabilities=edge.capabilities)
            for edge in self._edges.list_edges()
            if host_limit is None or matches_edge_limit(edge.name, host_limit)
        ]


def build_release_service(
    *,
    state: CanonicalState,
    releases: ReleaseStore,
    edges: EdgeStore,
    contributors: StateContributors,
    objections: Callable[[CdnSite], Sequence[ValidationIssue]],
    capabilities: Sequence[str],
    allow_empty_sites: bool,
    retention: int,
    uow: UnitOfWork,
    renderer: ConfigurationRenderer,
) -> ReleaseService:
    """Wire the compiler's inputs; ``retention`` is the history bound."""
    return ReleaseService(
        state=state,
        targets=StoredEdgeTargets(edges),
        releases=releases,
        contributors=contributors,
        site_checks=FeatureSiteChecks(objections),
        capabilities=sorted(capabilities),
        allow_empty_sites=allow_empty_sites,
        uow=uow,
        retention=retention,
        renderer=renderer,
    )
