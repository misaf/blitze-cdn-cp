"""Read-only reporting: doctor, audit, and the API server.

`stats` was here and is not any more. It reads the cache capability's report,
so it belongs to the distribution that produces one — this capability was
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

from blitzecdn import ansible as core_ansible
from blitzecdn.api import create_app
from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.core.plugins import (
    CapabilitySetting,
    EnvironmentKey,
    load_plugins,
    resolve_edge_capability_roles,
    resolve_host_capability_roles,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)

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

    `configuration` is the other half of the same question: which `BLITZE_*`
    names this capability claims, and whether this controller has set them.
    That answer used to exist only inside each package, so "I installed
    blitzecdn-geoip and no database is provisioned" had no command to ask —
    the names were in a README, and whether they had arrived was visible only
    in a failed play.
    """
    control = common.control_plane()
    registry = control.plugins
    secrets: dict[str, list[EnvironmentKey]] = {}
    settings: dict[str, list[CapabilitySetting]] = {}
    for contribution in registry.configuration_contributions():
        secrets.setdefault(contribution.plugin, []).extend(
            contribution.environment_keys
        )
        settings.setdefault(contribution.plugin, []).extend(contribution.settings)
    document: dict[str, Any] = {
        "plugins": [
            {
                "name": plugin.name,
                "version": plugin.version,
                "required": plugin.required,
                "capabilities": sorted(plugin.capabilities),
                "summary": plugin.summary,
                "configuration": [
                    {
                        "name": key.name,
                        # Which of the two kinds this is, rather than a
                        # boolean called `secret` — there are exactly two, an
                        # operator reads the word, and the field name is not
                        # then one a credential scanner has to be told about.
                        "kind": "secret",
                        "required": key.required,
                        "minimum_bytes": key.minimum_bytes,
                        # Whether a value arrived, never the value: this prints
                        # to a terminal and into whatever captures its JSON.
                        "set": control.capability_config.for_plugin(plugin.name).is_set(
                            key.name
                        ),
                        "summary": key.summary,
                    }
                    for key in sorted(
                        secrets.get(plugin.name, ()), key=lambda key: key.name
                    )
                ]
                + [
                    {
                        "name": setting.name,
                        "kind": "setting",
                        # The resolved value, which a setting may show and a
                        # secret may not — that difference is the whole reason
                        # the two are declared as different things.
                        "value": str(
                            control.capability_config.for_plugin(plugin.name).settings[
                                setting.name
                            ]
                        ),
                        "summary": setting.summary,
                    }
                    for setting in sorted(
                        settings.get(plugin.name, ()), key=lambda item: item.name
                    )
                ],
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


@ansible_app.command("roles-path")
def ansible_roles_path() -> None:
    """Print `ANSIBLE_ROLES_PATH` for this set of installed capabilities.

    Core's roles first, then each contributing distribution's, ordered by
    plugin name — the exact value the composition root hands the runner, from
    the exact function that composes it. A caller invoking `ansible-playbook`
    directly needs that same path, and until this command existed the only way
    to get one was to write it out again: the justfile spelled all nine
    directories by hand, so a checkout could syntax-check an edge play against
    a role set no deployment would ever resolve, and nothing would say so.

    Composing it here rather than restating it also brings the refusals along.
    A capability shipping a role name core already owns is caught by the same
    check that catches it at startup, in a command that costs nothing to run,
    rather than by an operator noticing that `blitzecdn_nginx` renders
    something nobody wrote.

    The plugins are loaded here rather than taken from a control plane because
    the answer depends only on what is installed: this runs in a lint step,
    where there is no database to open and no fleet to read.
    """
    search = resolve_role_search_path(
        core_ansible.ROLES_PATH, load_plugins().ansible_contributions()
    )
    typer.echo(":".join(str(path) for path in search))


@ansible_app.command("slots")
def ansible_slots() -> None:
    """Print the capability role lists core's own plays run, as extra-vars.

    Three slots, one document, in the form `ansible-playbook --extra-vars`
    takes:

        ansible-playbook edge.yml --extra-vars "$(blitzecdn ansible slots)"

    A slot list is not something a caller can write out correctly by reading
    the repository, which is what separates this from the search path above.
    Whether a role converges in the edge slot, the host slot or the
    decommission slot is a fact the *contributing package* declares, and the
    justfile's hand-written copy had already drifted from it in both
    directions: `blitzecdn_resolver` declares an edge role and was missing, and
    the teardown slot was not passed at all, so the decommission play was never
    syntax-checked with a capability in it.

    Empty lists are emitted rather than omitted. Core's plays default them, so
    an absent key and an empty one converge identically — but a caller
    diffing this output wants to see that a slot is empty rather than guess
    whether the question was asked.
    """
    contributions = load_plugins().ansible_contributions()
    common.emit(
        {
            "blitzecdn_capability_roles": list(
                resolve_edge_capability_roles(contributions)
            ),
            "blitzecdn_host_capability_roles": list(
                resolve_host_capability_roles(contributions)
            ),
            "blitzecdn_teardown_capability_roles": list(
                resolve_teardown_capability_roles(contributions)
            ),
        },
        json_output=True,
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
