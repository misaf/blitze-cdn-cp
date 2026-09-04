"""Manage non-secret fleet-wide Ansible policy."""

from __future__ import annotations

from typing import Annotated

import typer
import yaml

from blitzecdn.cli import common
from blitzecdn.core.domain.validation import validate_setting_name

config_app = typer.Typer(no_args_is_help=True, help="Manage global edge policy.")


def _name(value: str) -> str:
    """The domain rule, rendered as a Typer parameter error.

    The rule itself lives in ``domain.validation`` and is enforced by the store,
    because these rows are published to every host at inventory precedence and
    the constraint therefore belongs to the setting rather than to this command.
    This only translates the refusal into the message shape a CLI user expects.
    """
    try:
        return validate_setting_name(value)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


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
