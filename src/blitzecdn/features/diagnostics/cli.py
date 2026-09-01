"""Read-only reporting: doctor, audit, and the API server.

`stats` was here and is not any more. It reads the cache capability's report,
so it belongs to the distribution that produces one — this feature was
importing `CacheStatsReport` to format a document it did not own, which is the
exact edge that would have made `cache` undetachable.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated, Any

import dns.exception
import dns.rdatatype
import dns.resolver
import typer
import uvicorn

from blitzecdn.api import create_app
from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode

#: Root-level verbs, like the deployment group: `blitzecdn status`, not
#: `blitzecdn diagnostics status`.
diagnostics_app = typer.Typer()


@dataclass(frozen=True, slots=True)
class ResolverCheck:
    passed: bool
    detail: str


def check_resolver(settings: Any) -> ResolverCheck:
    """Detect a resolver that invents answers for reserved names."""
    servers = settings.preflight_dns_servers
    where = f" ({', '.join(servers)})" if servers else " (host resolver)"
    resolver = dns.resolver.Resolver()
    timeout = float(settings.preflight_dns_timeout_seconds)
    resolver.lifetime = timeout
    resolver.timeout = timeout
    if servers:
        resolver.nameservers = list(servers)
    probe = f"blitzecdn-resolver-probe-{secrets.token_hex(8)}.invalid"
    try:
        answer = resolver.resolve(probe, dns.rdatatype.A)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return ResolverCheck(True, f"resolver{where} rejects names that cannot exist")
    except dns.exception.DNSException:
        return ResolverCheck(True, f"resolver{where} did not answer the probe")
    addresses = ", ".join(sorted(str(record.address) for record in answer))
    return ResolverCheck(
        False,
        f"resolver{where} answered {addresses} for a reserved .invalid name, so "
        "it invents addresses instead of resolving",
    )


@diagnostics_app.command()
def plugins(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the capabilities this controller has, and any that failed to load.

    The answer to "I installed blitzecdn-cache and there is no `cache purge`".
    A required capability that failed would have stopped the process, but an
    optional one is reported and skipped by design, and a warning that scrolled
    past at startup is not somewhere an operator can look afterwards — so the
    reason is kept on the registry and printed here.

    `required` distinguishes a capability this distribution ships from one
    installed beside it. `capabilities` are the tokens configuration depends on
    through `required_capabilities`, which is usually just the plugin's name.
    """
    registry = common.control_plane().plugins
    document: dict[str, Any] = {
        "plugins": [
            {
                "name": plugin.name,
                "version": plugin.version,
                "required": plugin.required,
                "capabilities": sorted(plugin.capabilities),
                "summary": plugin.summary,
            }
            for plugin in registry.plugins
        ],
        "capabilities": sorted(registry.capabilities),
        "rejected": [
            {"source": rejection.source, "reason": rejection.reason}
            for rejection in registry.rejected
        ],
    }
    common.emit(document, json_output=json_output)
    if json_output:
        return
    for rejection in registry.rejected:
        typer.echo(
            f"\n{rejection.source} was not registered: {rejection.reason}", err=True
        )


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
    report = {
        "python_supported": True,
        "state_dir": str(settings.state_dir),
        "api_auth_configured": bool(settings.api_keys),
        "configuration_errors": settings.validate_runtime(),
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
    if report["configuration_errors"]:
        raise typer.Exit(ExitCode.CONFIGURATION)


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
