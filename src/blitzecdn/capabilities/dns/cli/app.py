"""The three command groups, and the helpers every policy command uses.

Separate from the commands so that a command module can import a group without
importing its siblings. Nothing here is a command; `__init__` imports the
modules that are, and each of those registers against one of these.
"""

from __future__ import annotations

import typer

from blitzecdn.capabilities.dns.domain import Domain, DomainPatch
from blitzecdn.cli import common

domain_app = typer.Typer(
    no_args_is_help=True,
    help="Manage DNS zones and the policy their hostnames are served with.",
)
record_app = typer.Typer(
    no_args_is_help=True,
    help="Manage DNS records and whether the edge serves each hostname.",
)
rule_app = typer.Typer(
    no_args_is_help=True,
    help="Override a zone's policy for some of its hostnames.",
)
dns_app = typer.Typer(no_args_is_help=True, help="Export DNS state.")

__all__ = ["dns_app", "domain_app", "record_app", "rule_app"]


def _update(name: str, patch: DomainPatch) -> Domain:
    return common.control_plane().dns.update_domain(name, patch, "cli")


def _report(zone: Domain, message: str, *, json_output: bool) -> None:
    """Print the zone, then say what it now does and whether it is served.

    Every policy command ends this way, so it ends here rather than in each of
    them. Not `common.emit(note=...)`: composing the note means asking the
    control plane whether anything in the zone is proxied, and a `--json`
    caller should not pay for a query whose only output is a sentence it
    discards.
    """
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(_applied(zone, message))


def _applied(zone: Domain, message: str) -> str:
    """A confirmation that says whether anything is actually serving yet.

    A zone's policy applies to the hostnames in it that are proxied, so a zone
    with none is configuration with nothing behind it. Saying so here is what
    stops an operator tuning cache headers on a zone that serves no traffic and
    wondering why nothing changed.
    """
    if any(
        record.proxied for record in common.control_plane().dns.list_records(zone.name)
    ):
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} Nothing in {zone.name!r} is proxied yet, so nothing is "
        "served — put a hostname on the edge with 'blitzecdn record proxy'."
    )
