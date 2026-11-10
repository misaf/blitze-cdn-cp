"""`rule` — overrides that apply to some hostnames in a zone and not others.

Settings are given as ``--set name=value``, since a rule holds whichever
settings it overrides, and validated against the API's ``DomainPatch``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from blitzecdn.capabilities.dns.cli.app import rule_app
from blitzecdn.capabilities.dns.domain import Rule, RulePatch
from blitzecdn.capabilities.dns.service import RuleService
from blitzecdn.core.domain.paths import PathRedirect
from blitzecdn.delivery.cli import common

_SET_HELP = (
    "A zone setting to override, as name=value. Repeatable. Values are read "
    "as JSON when they parse as JSON and as text otherwise, so "
    "cache_enabled=false is a boolean and cache_valid_success=10m is a string."
)


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """``name=value`` pairs, JSON-decoded where possible so ``false`` stays a bool."""
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


_PATH_HELP = (
    "Make this a path rule: a prefix ending in '/' (/static/) or an exact "
    "path (/robots.txt). It may then --set only path-scoped settings, or "
    "--redirect."
)
_REDIRECT_HELP = (
    "Redirect what a path rule matches to this URL or path. On a prefix path, "
    "$1 is what followed the prefix: --redirect 'https://new.example.com/$1'."
)
_STATUS_HELP = "The redirect's status: 301, 302, 307 or 308."
_QUERY_HELP = "Whether a redirect appends the visitor's query string."


def _redirect(
    target: str | None, status: int | None, keep_query: bool | None
) -> PathRedirect | None:
    """The redirect the options describe, or ``None`` without a target."""
    if target is None:
        if status is not None or keep_query is not None:
            raise typer.BadParameter(
                "--redirect-status and --keep-query need --redirect"
            )
        return None
    return PathRedirect.model_validate(
        {
            "target": target,
            **({} if status is None else {"status": status}),
            **({} if keep_query is None else {"preserve_query_string": keep_query}),
        }
    )


@rule_app.command("add")
def rule_add(
    domain: Annotated[str, typer.Argument(help="Zone the rule belongs to.")],
    name: Annotated[str, typer.Argument(help="Name for the rule within the zone.")],
    set_: Annotated[list[str] | None, typer.Option("--set", help=_SET_HELP)] = None,
    match: Annotated[
        str,
        typer.Option(
            "--match",
            help="Hostname this covers: '*', an exact name, or '*.suffix'.",
        ),
    ] = "*",
    path: Annotated[str | None, typer.Option("--path", help=_PATH_HELP)] = None,
    redirect: Annotated[
        str | None, typer.Option("--redirect", help=_REDIRECT_HELP)
    ] = None,
    redirect_status: Annotated[
        int | None, typer.Option("--redirect-status", help=_STATUS_HELP)
    ] = None,
    keep_query: Annotated[
        bool | None, typer.Option("--keep-query/--drop-query", help=_QUERY_HELP)
    ] = None,
    priority: Annotated[
        int,
        typer.Option("--priority", help="Lower runs first. The first match wins."),
    ] = 100,
    json_output: common.JsonOutput = False,
) -> None:
    """Override some of a zone's policy for the hostnames, or paths, a rule matches."""
    common.emit(
        common.service(RuleService).create_rule(
            Rule(
                domain=domain,
                name=name,
                match=match,
                path=path,
                priority=priority,
                overrides=_parse_overrides(set_ or []),
                redirect=_redirect(redirect, redirect_status, keep_query),
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
    common.emit(common.service(RuleService).list_rules(domain), json_output=json_output)


@rule_app.command("show")
def rule_show(
    domain: str,
    name: str,
    json_output: common.JsonOutput = False,
) -> None:
    """Show one rule: what it matches, and what it changes."""
    common.emit(
        common.service(RuleService).get_rule(domain, name), json_output=json_output
    )


@rule_app.command("set")
def rule_set(
    domain: str,
    name: str,
    set_: Annotated[list[str] | None, typer.Option("--set", help=_SET_HELP)] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    path: Annotated[str | None, typer.Option("--path", help=_PATH_HELP)] = None,
    no_path: Annotated[
        bool, typer.Option("--no-path", help="Make this a hostname rule again.")
    ] = False,
    redirect: Annotated[
        str | None, typer.Option("--redirect", help=_REDIRECT_HELP)
    ] = None,
    redirect_status: Annotated[
        int | None, typer.Option("--redirect-status", help=_STATUS_HELP)
    ] = None,
    keep_query: Annotated[
        bool | None, typer.Option("--keep-query/--drop-query", help=_QUERY_HELP)
    ] = None,
    no_redirect: Annotated[
        bool, typer.Option("--no-redirect", help="Remove the rule's redirect.")
    ] = False,
    priority: Annotated[int | None, typer.Option("--priority")] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Change a rule. Any --set replaces every override the rule had.

    Replaced rather than merged so that a setting can be taken back off a rule:
    if --set were additive there would be no way to stop overriding one. A
    --redirect likewise replaces the whole redirect.
    """
    if path is not None and no_path:
        raise typer.BadParameter("pass --path or --no-path, not both")
    if redirect is not None and no_redirect:
        raise typer.BadParameter("pass --redirect or --no-redirect, not both")
    changes: dict[str, Any] = {}
    if set_ is not None:
        changes["overrides"] = _parse_overrides(set_)
    if match is not None:
        changes["match"] = match
    if path is not None or no_path:
        changes["path"] = path
    parsed = _redirect(redirect, redirect_status, keep_query)
    if parsed is not None or no_redirect:
        changes["redirect"] = parsed
    if priority is not None:
        changes["priority"] = priority
    if enabled is not None:
        changes["enabled"] = enabled
    if not changes:
        raise typer.BadParameter(
            "nothing to change; pass --set, --match, --path, --redirect, "
            "--priority, or --enabled/--disabled"
        )
    common.emit(
        common.service(RuleService).update_rule(
            domain, name, RulePatch.model_validate(changes), "cli"
        ),
        json_output=json_output,
    )


@rule_app.command("remove")
def rule_remove(
    domain: str,
    name: str,
    yes: common.Yes = False,
) -> None:
    """Remove a rule. The hostnames it covered fall back to the zone's policy."""
    if not yes and not typer.confirm(f"Delete rule {name!r} in {domain!r}?"):
        raise typer.Abort()
    common.service(RuleService).delete_rule(domain, name, "cli")
    typer.echo(f"Deleted rule {name} in {domain}")
