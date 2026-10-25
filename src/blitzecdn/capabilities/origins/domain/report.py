"""What the fleet says about the origins it proxies to.

Checked from the edges, not the controller, whose network is not the one
carrying traffic: an origin allow-listing the edges refuses the controller.
Reported per edge and per site, since reachable from three edges of four is
the case worth surfacing.
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
    """Every edge's answer, read per site: all reach it, none do, or some do."""

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
        """Sites with a failing origin, and the edges that could not reach it."""
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
