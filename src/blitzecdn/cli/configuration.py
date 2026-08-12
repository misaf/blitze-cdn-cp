"""Manage non-secret fleet-wide Ansible policy."""

from __future__ import annotations

from typing import Annotated

import typer
import yaml

from blitzecdn.cli import common

config_app = typer.Typer(no_args_is_help=True, help="Manage global edge policy.")

RESERVED_SETTINGS = {
    "blitzecdn_firewall_ssh_port",
    "blitzecdn_firewall_ssh_sources",
    "blitzecdn_public_addresses",
    "blitzecdn_nginx_sites",
}


def _name(value: str) -> str:
    if not value.startswith("blitzecdn_"):
        raise typer.BadParameter("setting names must start with 'blitzecdn_'")
    if value in RESERVED_SETTINGS:
        raise typer.BadParameter(
            f"{value} is derived from edge or desired-state records"
        )
    if any(word in value.lower() for word in ("password", "secret", "token", "key")):
        raise typer.BadParameter("secrets and keys belong in .env, not the database")
    return value


@config_app.command("list")
def list_settings(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List database-backed global edge settings."""
    common.emit(
        common.control_plane().ansible_settings.list_settings(),
        json_output=json_output,
    )


@config_app.command("set")
def set_setting(name: str, value: str) -> None:
    """Set a value; VALUE uses YAML syntax (true, 42, [one, two], etc.)."""
    parsed = yaml.safe_load(value)
    if parsed is None:
        raise typer.BadParameter("value must not be null")
    common.control_plane().ansible_settings.set_setting(_name(name), parsed)
    typer.echo(f"Set {name}")


@config_app.command("unset")
def unset_setting(name: str) -> None:
    """Remove an override and fall back to the shipped default."""
    common.control_plane().ansible_settings.delete_setting(_name(name))
    typer.echo(f"Unset {name}")
