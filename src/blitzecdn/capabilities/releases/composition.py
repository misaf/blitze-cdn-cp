"""How the releases capability is built.

The compiler is a pure function, so almost all of this is deciding what its
arguments are: which capabilities are installed, which edges a run would reach,
who answers a plugin's site check, and where compiled releases are kept. Every
one of those is a composition decision, which is why they are settled here and
not inside the service.

The two adapters below exist for the same reason. Both bind something the
composition root has — the plugin registry, the edge store — to a port this
capability declared, and neither has any behaviour a capability test would want
to fake around.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.capabilities.releases.domain import EdgeCapabilities, ReleaseFinding
from blitzecdn.capabilities.releases.ports import CanonicalState, ReleaseStore
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.core.domain.validation import matches_edge_limit
from blitzecdn.core.plugins import PluginRegistry, Severity

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

__all__ = ["PluginSiteChecks", "StoredEdgeTargets", "build_release_service"]

_LOGGER = logging.getLogger(__name__)


class PluginSiteChecks:
    """The compiler's view of "what do the installed capabilities object to".

    Two jobs, and the second is why this is a class rather than a bound method.
    A plugin issue carries a severity: a blocking one must refuse the
    deployment, and a warning must be logged and converged anyway. The compiler
    has no logger and must not acquire one — it is a pure function — so the
    warning is emitted here and only the blocking issues become findings.

    ``site_objections`` rather than ``validate_site``: the latter also reports
    capabilities this installation does not have, and the compiler makes that
    check itself — against the selected edges as well as against the controller
    — so asking for both would report every missing capability twice. It also
    matters which kind a finding is: an objection stops the artifact being
    rendered, and a missing capability does not.

    Attribution survives into the message because the operator reading a
    refusal may have installed the objecting package yesterday and has to know
    which one is objecting.
    """

    def __init__(self, plugins: PluginRegistry, platform: ControlPlane) -> None:
        self._plugins = plugins
        self._platform = platform

    def __call__(self, site: CdnSite) -> Sequence[ReleaseFinding]:
        findings: list[ReleaseFinding] = []
        for issue in self._plugins.site_objections(site, self._platform):
            message = f"{issue.plugin}: {issue.site}: {issue.message}"
            if issue.severity is Severity.BLOCKING:
                findings.append(
                    ReleaseFinding(source=issue.plugin, host=site.name, message=message)
                )
            else:
                _LOGGER.warning("%s", message)
        return findings


class StoredEdgeTargets:
    """The registered fleet, narrowed to the edges a run would actually reach.

    ``host_limit`` is Ansible's ``--limit`` pattern, and it is applied here
    rather than passed through to the compiler because the compiler must not
    know what an Ansible limit is. The same matcher the runner uses decides,
    so "which edges will this run touch" has one answer whoever asks.
    """

    def __init__(self, edges: EdgeStore) -> None:
        self._edges = edges

    def targets(self, *, host_limit: str | None = None) -> Sequence[EdgeCapabilities]:
        return [
            EdgeCapabilities(name=edge.name, capabilities=edge.capabilities)
            for edge in self._edges.list_edges()
            if host_limit is None or matches_edge_limit(edge.name, host_limit)
        ]


def build_release_service(
    platform: ControlPlane,
    *,
    state: CanonicalState,
    releases: ReleaseStore,
    edges: EdgeStore,
) -> ReleaseService:
    """Wire the compiler's inputs to the installation they come from."""
    return ReleaseService(
        state=state,
        targets=StoredEdgeTargets(edges),
        releases=releases,
        contributors=platform.plugins.contributions_for(platform),
        site_checks=PluginSiteChecks(platform.plugins, platform),
        capabilities=sorted(platform.plugins.capabilities),
        allow_empty_sites=platform.settings.allow_empty_sites,
        uow=platform.transactions,
        # Releases are pruned on the same bound as deployment history, because
        # they are the thing that history points *at*: keeping fewer would
        # strand rollback targets, and keeping more would accumulate documents
        # nothing can reach.
        retention=platform.settings.history_retention,
    )
