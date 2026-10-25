"""The command groups dns founds, apart from the commands that register on them."""

from __future__ import annotations

import typer

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
