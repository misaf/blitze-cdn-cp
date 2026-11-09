"""How the releases capability is built: settling the pure compiler's inputs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from dishka import Provider, Scope, provide

from blitzecdn.assets import ansible as core_ansible
from blitzecdn.capabilities.dns import RuleReader, ZoneStore
from blitzecdn.capabilities.edges import EdgeStore
from blitzecdn.capabilities.releases.adapters.persistence import (
    ReleaseStore as ReleaseTable,
)
from blitzecdn.capabilities.releases.domain import (
    EdgeCapabilities,
    ReleaseFinding,
    ReleaseInputs,
)
from blitzecdn.capabilities.releases.ports import (
    CanonicalState,
    ConfigurationRenderer,
    ReleaseReferences,
    ReleaseStore,
    StateContributors,
)
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.rendering import EdgeRenderer
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.validation import matches_edge_limit
from blitzecdn.core.features import (
    FeatureRegistry,
    Rendering,
    Severity,
    ValidationIssue,
)
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import FleetSettings

__all__ = [
    "FeatureSiteChecks",
    "ReleasesProvider",
    "StoredCanonicalState",
    "StoredEdgeTargets",
    "build_release_service",
]

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


class StoredCanonicalState:
    """The zones, rules and fleet settings a release compiles, read in one
    transaction so no write between the reads can be observed."""

    def __init__(
        self,
        *,
        zones: ZoneStore,
        rules: RuleReader,
        settings: FleetSettings,
        uow: UnitOfWork,
    ) -> None:
        self._zones = zones
        self._rules = rules
        self._settings = settings
        self._uow = uow

    def release_inputs(self) -> ReleaseInputs:
        with self._uow.transaction():
            return ReleaseInputs.of(
                self._zones.list_domains(),
                self._zones.list_records(),
                self._rules.list_rules(),
                settings=self._settings.list_settings(),
            )


class ReleasesProvider(Provider):
    """The release table, the canonical state it compiles, and the compiler."""

    scope = Scope.APP

    @provide
    def releases(
        self, database: Database, referenced: ReleaseReferences
    ) -> ReleaseStore:
        return ReleaseTable(database, referenced)

    @provide
    def state(
        self,
        zones: ZoneStore,
        rules: RuleReader,
        settings: FleetSettings,
        uow: UnitOfWork,
    ) -> CanonicalState:
        return StoredCanonicalState(
            zones=zones, rules=rules, settings=settings, uow=uow
        )

    @provide
    def service(
        self,
        settings: Settings,
        state: CanonicalState,
        releases: ReleaseStore,
        features: FeatureRegistry,
        edges: EdgeStore,
        rendering: Rendering[CdnSite],
        edge_contributions: EdgeContributions,
        uow: UnitOfWork,
    ) -> ReleaseService:
        return build_release_service(
            state=state,
            releases=releases,
            edges=edges,
            contributors=rendering,
            objections=rendering.site_objections,
            capabilities=tuple(features.capabilities),
            allow_empty_sites=settings.allow_empty_sites,
            retention=settings.history_retention,
            uow=uow,
            # The shipped templates and defaults, from the package itself,
            # whatever directory its playbooks run from.
            renderer=EdgeRenderer(
                ansible_dir=core_ansible.ROLES_PATH.parent,
                contributions=edge_contributions,
            ),
        )
