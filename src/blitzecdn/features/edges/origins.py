"""What the edges learn by connecting to the origins they proxy to.

The check runs *on the fleet*, not on the controller. It used to run here, and
the answer it gave was about the controller's network: its routes, its
resolver, its egress firewall. None of those are the ones that matter. An
origin that allow-lists the edges' addresses refuses the controller, and one
reachable only from the controller's subnet passes a check and then 502s on
every edge — both of which are the check reporting confidently on a question
nobody asked.

So each edge answers for itself, through the same ``blitzecdn_report`` channel
the statistics role uses, and the report is per edge *and* per site: "the origin
is up" is not a fleet-wide fact, and an origin reachable from three edges out of
four is exactly the situation worth surfacing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.features.http.policy import HttpScheme
from blitzecdn.features.tls.policy import SslMode


class OriginCheck(BaseModel):
    """One edge's answer about one site's origin.

    Two separate answers, because the fixes differ. ``reachable`` false means
    DNS, routing, a firewall, or the wrong port — the edge got nothing back at
    all. ``tls_verified`` false with ``reachable`` true means the origin
    answered but presented a certificate that is not valid for the SNI this
    edge sends, which is the site's ``origin_sni`` or the origin's certificate,
    and nothing to do with the network. ``tls_verified`` is ``None`` for an HTTP
    origin or Full mode, where verification is deliberately not attempted.

    ``status`` is whatever the origin answered a ``HEAD /`` with, and is not
    judged: a 404 or a 403 still proves the origin is up and talking, which is
    the whole of what this check claims to know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    origin: str
    scheme: HttpScheme
    ssl_mode: SslMode
    sni: str | None = None
    reachable: bool = False
    tls_verified: bool | None = None
    #: The HTTP status the origin answered with, or ``None`` if it did not.
    status: int | None = None
    #: Present only for Automatic SSL/TLS scans, which compare the response
    #: reached over the current transport with the same response over HTTPS.
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tls_verified is not False


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
    """What the fleet says about the origins it proxies to.

    Read per site rather than per edge when deciding whether something is
    wrong: an origin every edge can reach is healthy, one no edge can reach is
    down, and one some edges can reach is a routing or allow-list problem that
    a single vantage point could never have told apart.
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
