"""Manage non-secret fleet-wide Ansible policy."""

from __future__ import annotations

import typer
import yaml

from blitzecdn.core.domain.validation import validate_setting_name
from blitzecdn.core.ports.operations import FleetSettings
from blitzecdn.entrypoints.cli import common

config_app = typer.Typer(no_args_is_help=True, help="Manage global edge policy.")


def _name(value: str) -> str:
    """The setting-name rule, as a Typer parameter error; the store enforces it."""
    try:
        return validate_setting_name(value)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@config_app.command("list")
def list_settings(
    json_output: common.JsonOutput = False,
) -> None:
    """List database-backed global edge settings."""
    common.emit(
        common.service(FleetSettings).list_settings(),
        json_output=json_output,
    )


@config_app.command("set")
def set_setting(name: str, value: str) -> None:
    """Set a value; VALUE uses YAML syntax (true, 42, [one, two], etc.)."""
    parsed = yaml.safe_load(value)
    if parsed is None:
        raise typer.BadParameter("value must not be null")
    common.service(FleetSettings).set_setting(_name(name), parsed)
    typer.echo(f"Set {name}; it reaches the edges with the next deployment.")


@config_app.command("unset")
def unset_setting(name: str) -> None:
    """Remove an override and fall back to the shipped default."""
    common.service(FleetSettings).delete_setting(_name(name))
    typer.echo(f"Unset {name}; it reaches the edges with the next deployment.")
