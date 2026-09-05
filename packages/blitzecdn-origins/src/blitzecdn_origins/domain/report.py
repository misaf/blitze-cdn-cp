"""What the fleet says about the origins it proxies to.

The check runs *on the fleet*, not on the controller. It used to run on the
controller, and the answer it gave was about the controller's network: its
routes, its resolver, its egress firewall. None of those are the ones that
matter. An origin that allow-lists the edges' addresses refuses the controller,
and one reachable only from the controller's subnet passes a check and then
502s on every edge — both of which are the check reporting confidently on a
question nobody asked.

So each edge answers for itself, through the same ``blitzecdn_report`` channel
the statistics role uses, and the report is per edge *and* per site: "the origin
is up" is not a fleet-wide fact, and an origin reachable from three edges out of
four is exactly the situation worth surfacing.

``OriginCheck`` — one edge's answer about one origin — is deliberately *not*
here. It stays in the control plane because the controller's own advisory probe
inside certificate preflight produces one without ever running this play, so a
row shape that travelled with this wheel would take that probe's return type
away from a controller that had detached it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.edges.domain.origins import OriginCheck


class EdgeOriginChecks(BaseModel):
    """One edge's answer about every origin it was asked to reach."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    checked_at: datetime | None = None
    checks: tuple[OriginCheck, ...] = ()
    #: Why this edge said nothing usable, when it did not.
    error: str | None = None

    @property
    def reporting(self) -> bool:
        return self.error is None

    @property
    def failing(self) -> tuple[OriginCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)


class OriginReport(BaseModel):
    """Every edge's answer, read per site rather than per edge.

    Which is how to decide whether something is wrong: an origin every edge can
    reach is healthy, one no edge can reach is down, and one some edges can
    reach is a routing or allow-list problem that a single vantage point could
    never have told apart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeOriginChecks, ...] = ()

    @property
    def reporting(self) -> tuple[EdgeOriginChecks, ...]:
        return tuple(edge for edge in self.edges if edge.reporting)

    @property
    def silent(self) -> tuple[EdgeOriginChecks, ...]:
        return tuple(edge for edge in self.edges if not edge.reporting)

    @property
    def failing_sites(self) -> dict[str, tuple[str, ...]]:
        """Sites with a failing origin, and the edges that could not reach it.

        The shape the answer is wanted in: a site name is what an operator is
        looking up, and the list of edges beside it is what says whether this is
        the origin being down or the path from one edge being broken.
        """
        failures: dict[str, list[str]] = {}
        for edge in self.reporting:
            for check in edge.failing:
                failures.setdefault(check.site, []).append(edge.host)
        return {site: tuple(hosts) for site, hosts in sorted(failures.items())}

    @property
    def healthy(self) -> bool:
        """Every edge that answered could reach every origin it was given."""
        return bool(self.reporting) and not self.failing_sites


__all__ = ["EdgeOriginChecks", "OriginReport"]
