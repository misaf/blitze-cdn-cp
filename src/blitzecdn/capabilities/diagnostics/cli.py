"""Read-only controller reporting: doctor, audit, features and the API server."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated, Any

import dns.exception
import dns.rdatatype
import dns.resolver
import typer
import uvicorn

from blitzecdn.assets import ansible as core_ansible
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.core.features import (
    CapabilitySetting,
    EnvironmentKey,
    FeatureRegistry,
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.ports.operations import AuditTrail
from blitzecdn.entrypoints.api import create_app
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.common import ExitCode

#: Root-level verbs, like the deployment group: `blitzecdn status`, not
#: `blitzecdn diagnostics status`.
diagnostics_app = typer.Typer()

#: `blitzecdn ansible ...`: what the installed capabilities contribute to the
#: two process-wide Ansible values core composes for itself at run time.
ansible_app = typer.Typer(
    no_args_is_help=True,
    help="The Ansible inputs composed from the installed capabilities.",
)


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
def features(
    json_output: common.JsonOutput = False,
) -> None:
    """List the features this controller ships and the configuration they claim.

    `capabilities` are the tokens a site's settings and an edge's runtime are
    checked against, which is usually just the feature's name.

    `configuration` is the other half of the same question: which `BLITZE_*`
    names each feature claims, and whether this controller has set them.
    Asked here rather than left inside each feature, where "no GeoIP database
    is provisioned" has no command to ask: the names live in a README, and
    whether they arrived is visible only in a failed play.
    """
    registry = common.service(FeatureRegistry)
    configured = common.service(ResolvedCapabilityEnvironment)
    secrets: dict[str, list[EnvironmentKey]] = {}
    settings: dict[str, list[CapabilitySetting]] = {}
    for contribution in registry.configuration_contributions():
        secrets.setdefault(contribution.feature, []).extend(
            contribution.environment_keys
        )
        settings.setdefault(contribution.feature, []).extend(contribution.settings)
    document: dict[str, Any] = {
        "features": [
            {
                "name": feature.name,
                "version": feature.version,
                "capabilities": sorted(feature.capabilities),
                "summary": feature.summary,
                "configuration": [
                    {
                        "name": key.name,
                        # The kind as a word, not a boolean `secret`.
                        "kind": "secret",
                        "required": key.required,
                        "minimum_bytes": key.minimum_bytes,
                        # Whether a value arrived, never the value: this prints
                        # to a terminal and into whatever captures its JSON.
                        "set": configured.for_feature(feature.name).is_set(key.name),
                        "summary": key.summary,
                    }
                    for key in sorted(
                        secrets.get(feature.name, ()), key=lambda key: key.name
                    )
                ]
                + [
                    {
                        "name": setting.name,
                        "kind": "setting",
                        # A setting may show its value; a secret may not.
                        "value": str(
                            configured.for_feature(feature.name).settings[setting.name]
                        ),
                        "summary": setting.summary,
                    }
                    for setting in sorted(
                        settings.get(feature.name, ()), key=lambda item: item.name
                    )
                ],
            }
            for feature in registry.metadata
        ],
        "capabilities": sorted(registry.capabilities),
    }
    common.emit(document, json_output=json_output)


@ansible_app.command("roles-path")
def ansible_roles_path() -> None:
    """Print `ANSIBLE_ROLES_PATH` for this set of installed capabilities.

    Core's roles first, then each feature's by name: the exact value the runner
    gets, from the same function, refusals included. Needs no database, so it
    runs in a lint step.
    """
    search = resolve_role_search_path(
        core_ansible.ROLES_PATH, common.installed_features().ansible_contributions()
    )
    typer.echo(":".join(str(path) for path in search))


@ansible_app.command("slots")
def ansible_slots() -> None:
    """Print the capability role lists core's own plays run, as extra-vars.

        ansible-playbook edge.yml --extra-vars "$(blitzecdn ansible slots)"

    Which slot a role converges in is the contributing feature's declaration.
    Empty lists are printed, not omitted.
    """
    contributions = common.installed_features().ansible_contributions()
    common.emit(
        {
            "blitzecdn_capability_roles": list(
                resolve_edge_capability_roles(contributions)
            ),
            "blitzecdn_host_capability_roles": list(
                resolve_host_capability_roles(contributions)
            ),
            "blitzecdn_edge_teardown_capability_roles": list(
                resolve_teardown_capability_roles(contributions)
            ),
        },
        json_output=True,
    )


@diagnostics_app.command()
def audit(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    json_output: common.JsonOutput = False,
) -> None:
    """Show immutable operator audit events."""
    common.emit(
        common.service(AuditTrail).list_audit_events(limit),
        json_output=json_output,
    )


@diagnostics_app.command()
def doctor(
    json_output: common.JsonOutput = False,
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
        # Who the API admits, so exposure is visible without reading the environment
        # file; empty means loopback only. The list, not a verdict: binding is the
        # entrypoint's choice.
        "api_allowed_ips": list(settings.allowed_ips),
        "configuration_errors": settings.validate_runtime(),
        # Stated outright: nothing publishes DNS, so a ready fleet still serves nobody
        # until something answers for its hostnames.
        "dns_publication": common.service(DnsService)
        .publication()
        .model_dump(mode="json"),
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
    uvicorn.run(
        create_app(settings), host=host, port=port, access_log=True, proxy_headers=False
    )
