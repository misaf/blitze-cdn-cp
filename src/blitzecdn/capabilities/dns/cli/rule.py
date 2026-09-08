"""`rule` — the overrides that apply to some hostnames in a zone and not others.

The settings are given as ``--set name=value`` rather than as twenty typed
flags, which is the opposite of how `domain` spells the same settings, so it is
worth saying why. A `domain` command sets one named setting and can say in its
signature what that setting takes. A rule holds *whichever* settings an
operator wants to override, so the equivalent would be twenty optional flags
per command with nineteen unset on every call — and the flag that is missing
from the list is invisible until somebody needs it.

The values are validated against the same ``DomainPatch`` the API uses, so
``--set cache_valid_success=nonsense`` is refused here for the reason it would
be refused there, with the same message.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from blitzecdn.capabilities.dns.cli.app import rule_app
from blitzecdn.capabilities.dns.domain import Rule, RulePatch
from blitzecdn.cli import common

_SET_HELP = (
    "A zone setting to override, as name=value. Repeatable. Values are read "
    "as JSON when they parse as JSON and as text otherwise, so "
    "cache_enabled=false is a boolean and cache_valid_success=10m is a string."
)


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """``name=value`` into the mapping a rule stores.

    JSON first so that ``false``, ``0`` and ``null`` arrive as themselves
    rather than as the strings that spell them; plain text otherwise, because
    every duration and hostname a setting takes would need quoting if not.
    """
    overrides: dict[str, Any] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator:
            raise typer.BadParameter(f"expected name=value, got {pair!r}")
        try:
            overrides[name.strip()] = json.loads(value)
        except json.JSONDecodeError:
            overrides[name.strip()] = value
    return overrides


@rule_app.command("add")
def rule_add(
    domain: Annotated[str, typer.Argument(help="Zone the rule belongs to.")],
    name: Annotated[str, typer.Argument(help="Name for the rule within the zone.")],
    set_: Annotated[list[str], typer.Option("--set", help=_SET_HELP)],
    match: Annotated[
        str,
        typer.Option(
            "--match",
            help="Hostname this covers: '*', an exact name, or '*.suffix'.",
        ),
    ] = "*",
    priority: Annotated[
        int,
        typer.Option("--priority", help="Lower runs first. The first match wins."),
    ] = 100,
    json_output: common.JsonOutput = False,
) -> None:
    """Override some of a zone's policy for the hostnames a rule matches."""
    common.emit(
        common.control_plane().rules.create_rule(
            Rule(
                domain=domain,
                name=name,
                match=match,
                priority=priority,
                overrides=_parse_overrides(set_),
            ),
            "cli",
        ),
        json_output=json_output,
    )


@rule_app.command("list")
def rule_list(
    domain: Annotated[str, typer.Argument(help="Zone to list the rules of.")],
    json_output: common.JsonOutput = False,
) -> None:
    """List a zone's rules in the order a hostname is matched against them."""
    common.emit(
        common.control_plane().rules.list_rules(domain), json_output=json_output
    )


@rule_app.command("show")
def rule_show(
    domain: str,
    name: str,
    json_output: common.JsonOutput = False,
) -> None:
    """Show one rule: what it matches, and what it changes."""
    common.emit(
        common.control_plane().rules.get_rule(domain, name), json_output=json_output
    )


@rule_app.command("set")
def rule_set(
    domain: str,
    name: str,
    set_: Annotated[list[str] | None, typer.Option("--set", help=_SET_HELP)] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    priority: Annotated[int | None, typer.Option("--priority")] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Change a rule. Any --set replaces every override the rule had.

    Replaced rather than merged so that a setting can be taken back off a rule:
    if --set were additive there would be no way to stop overriding one.
    """
    changes: dict[str, Any] = {}
    if set_ is not None:
        changes["overrides"] = _parse_overrides(set_)
    if match is not None:
        changes["match"] = match
    if priority is not None:
        changes["priority"] = priority
    if enabled is not None:
        changes["enabled"] = enabled
    if not changes:
        raise typer.BadParameter(
            "nothing to change; pass --set, --match, or --priority"
        )
    common.emit(
        common.control_plane().rules.update_rule(
            domain, name, RulePatch.model_validate(changes), "cli"
        ),
        json_output=json_output,
    )


@rule_app.command("remove")
def rule_remove(
    domain: str,
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a rule. The hostnames it covered fall back to the zone's policy."""
    if not yes and not typer.confirm(f"Delete rule {name!r} in {domain!r}?"):
        raise typer.Abort()
    common.control_plane().rules.delete_rule(domain, name, "cli")
    typer.echo(f"Deleted rule {name} in {domain}")
