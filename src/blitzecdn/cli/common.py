"""Helpers every command group shares.

Commands call ``common.control_plane()`` through this module rather than
importing the function by name, so a test that swaps it out has one place to
patch and every command group sees the change.
"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any

import typer
import yaml

from blitzecdn.config import Settings
from blitzecdn.control_plane import ControlPlane, build_control_plane


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    CONFIGURATION = 3
    CONFLICT = 4
    DEPLOYMENT_FAILED = 5
    #: `drift` found a reachable edge that no longer matches desired state, or
    #: an edge it could not reach. Distinct from DEPLOYMENT_FAILED so a
    #: scheduled check can tell "the fleet moved" from "the check itself broke".
    DRIFT_DETECTED = 6


def settings() -> Settings:
    return Settings.from_environment()


def control_plane() -> ControlPlane:
    return build_control_plane(settings())


def emit(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
    if json_output:
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
    else:
        # Every caller hands this a model, a list, or a dict, and safe_dump
        # renders all three. There is deliberately no scalar branch: a command
        # that wants to print a bare string calls typer.echo itself.
        typer.echo(yaml.safe_dump(value, sort_keys=False).rstrip())


LIMIT_OPTION = typer.Option(
    "--limit",
    help=(
        "Restrict this run to these edges: names or globs, comma separated. "
        "Resolved against the inventory, so it can only ever narrow the fleet."
    ),
)
