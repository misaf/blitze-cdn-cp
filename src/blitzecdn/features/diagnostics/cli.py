"""Read-only reporting: doctor, audit, stats, and the API server."""

from __future__ import annotations

from typing import Annotated, Any

import typer
import uvicorn

from blitzecdn.api import create_app
from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.features.cache.domain import CacheStatsReport
from blitzecdn.features.certificates import check_resolver
from blitzecdn.features.certificates.domain import CERTIFICATE_RENEWAL_DAYS

#: Root-level verbs, like the deployment group: `blitzecdn status`, not
#: `blitzecdn diagnostics status`.
diagnostics_app = typer.Typer()


@diagnostics_app.command()
def audit(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show immutable operator audit events."""
    common.emit(
        common.control_plane().audit.list_audit_events(limit),
        json_output=json_output,
    )


@diagnostics_app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    resolver_check: Annotated[
        bool,
        typer.Option(
            "--resolver/--no-resolver",
            help="Probe the resolver for invented answers (one DNS query).",
        ),
    ] = True,
) -> None:
    """Report local readiness without contacting the edge servers.

    The resolver probe is the one thing here that leaves the machine: a single
    lookup of a reserved name that must not exist. It earns its place because a
    resolver that answers it is invisible to every other check while making all
    of them wrong. Pass --no-resolver on a host with no DNS at all.
    """
    settings = common.settings()
    # Certificate expiry is read from the local store, so it belongs in a
    # check that promises not to touch the network. It is also the thing most
    # likely to take a site down while every other check stays green.
    expiring = common.control_plane().certificates.expiring_certificates()
    report = {
        "python_supported": True,
        "state_dir": str(settings.state_dir),
        "api_auth_configured": bool(settings.api_keys),
        "configuration_errors": settings.validate_runtime(),
        "certificates_expiring": [
            {
                "site": status.site,
                "days_remaining": status.days_remaining,
                "renewable": status.renewable,
            }
            for status in expiring
        ],
    }
    resolver_report = check_resolver(settings) if resolver_check else None
    if resolver_report is not None:
        report["resolver"] = {
            "passed": resolver_report.passed,
            "detail": resolver_report.detail,
        }
    common.emit(report, json_output=json_output)
    if resolver_report is not None and not resolver_report.passed:
        typer.echo(f"\n{resolver_report.detail}.", err=True)
    if not json_output and expiring:
        typer.echo(
            f"\n{len(expiring)} certificate(s) expire within "
            f"{CERTIFICATE_RENEWAL_DAYS} days. Run 'blitzecdn cert renew'.",
            err=True,
        )
    if report["configuration_errors"]:
        raise typer.Exit(ExitCode.CONFIGURATION)


@diagnostics_app.command()
def stats(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    by_site: Annotated[
        bool,
        typer.Option("--by-site", help="Break the numbers down by virtual host."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report cache effectiveness across the edges.

    Reads the access log and nginx's own counters on each edge. Changes
    nothing, so it is safe to run at any time, including during a deploy.

    The hit ratio counts only requests that consulted the cache. Redirects,
    errors nginx served itself, and sites with caching disabled are excluded
    rather than scored as misses.
    """
    report = common.control_plane().cache.cache_stats("cli", host_limit=limit)
    if json_output:
        common.emit(_stats_document(report, by_site=by_site), json_output=True)
        return
    common.emit(_stats_document(report, by_site=by_site), json_output=False)
    for edge in report.silent:
        typer.echo(f"\n{edge.host} reported nothing: {edge.error}", err=True)
    if report.hit_ratio is None:
        typer.echo(
            "\nNo cacheable requests in the window read, so there is no hit "
            "ratio yet. A fresh edge or a quiet one both look like this."
        )


def _stats_document(report: CacheStatsReport, *, by_site: bool) -> dict[str, Any]:
    """One shape for both outputs, so --json is never a different answer."""
    document: dict[str, Any] = {
        "collected_at": report.collected_at.isoformat(),
        "hit_ratio": report.hit_ratio,
        "hits": report.hits,
        "cacheable_requests": report.cacheable_requests,
        "requests": report.requests,
        "edges": [
            {
                "host": edge.host,
                "hit_ratio": edge.hit_ratio,
                "requests": edge.requests,
                "nginx_reachable": edge.nginx_reachable,
                "connections": edge.connections,
                "error": edge.error,
            }
            for edge in report.edges
        ],
    }
    if by_site:
        document["sites"] = [
            {
                "site": site.site,
                "hit_ratio": site.hit_ratio,
                "hits": site.hits,
                "cacheable_requests": site.cacheable_requests,
                "requests": site.requests,
            }
            for site in report.by_site()
        ]
    return document


@diagnostics_app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Run the authenticated HTTP control plane."""
    settings = common.settings()
    if not settings.api_keys:
        raise typer.BadParameter("configure BLITZE_API_KEYS before starting the API")
    uvicorn.run(create_app(settings), host=host, port=port, access_log=True)
