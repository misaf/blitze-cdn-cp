"""Helpers every command group shares.

Commands call ``common.control_plane()`` through this module rather than
importing the function by name, so a test that swaps it out has one place to
patch and every command group sees the change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import IntEnum
from typing import Any

import typer
import yaml

from blitzecdn.composition import (
    ControlPlane,
    build_control_plane,
    load_control_plane_plugins,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.plugins import PluginRegistry


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
    #: The thing asked about does not exist. The CLI's 404.
    NOT_FOUND = 7
    #: A deployment is already running. The one failure here that clears on its
    #: own, so the one a scheduled caller should retry rather than alert on —
    #: which is why it is not folded into CONFLICT.
    BUSY = 8


def settings() -> Settings:
    return Settings.from_environment()


def control_plane() -> ControlPlane:
    return build_control_plane(settings())


def installed_plugins() -> PluginRegistry:
    """What is installed, for the commands whose answer needs nothing else.

    `blitzecdn ansible search-path` and `blitzecdn edges modules` run where
    there is no database to open and no fleet to read — a lint step, an image
    build — so they ask the registry rather than build a control plane to get
    at its. They come through here for the same reason every command comes
    through `control_plane()`: the composition root is named in one module of
    the command line, not in each command group that happens to need it.
    """
    return load_control_plane_plugins()


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


def describe_hosts(hosts: Sequence[HostRun]) -> str:
    """Render per-host results the way an operator wants to read them.

    Replaces echoing Ansible's own output. That output was thousands of lines
    of which a handful mattered, and it could not be filtered because nothing
    had parsed it — whereas the structured result already knows which tasks
    changed and which failed, so this prints those and nothing else.
    """
    lines: list[str] = []
    for host in hosts:
        if not host.reached:
            lines.append(f"  {host.host}: unreachable")
            continue
        if host.failures:
            lines.append(f"  {host.host}: {len(host.failures)} failed")
            lines.extend(
                f"      {failure.task}: {failure.message or failure.outcome.value}"
                for failure in host.failures
            )
            continue
        if not host.changed:
            lines.append(f"  {host.host}: in sync")
            continue
        lines.append(f"  {host.host}: would change {host.changed} task(s)")
        lines.extend(f"      {change.task}" for change in host.changes)
    return "\n".join(lines)


LIMIT_OPTION = typer.Option(
    "--limit",
    help=(
        "Restrict this run to these edges: names or globs, comma separated. "
        "Resolved against the inventory, so it can only ever narrow the fleet."
    ),
)
