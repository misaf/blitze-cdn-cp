"""Helpers every command group shares.

Commands call ``common.control_plane()`` through this module rather than
importing the function by name, so a test that swaps it out has one place to
patch and every command group sees the change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import IntEnum
from typing import Annotated, Any

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


#: The ``--json`` switch every command that prints a result carries. One
#: definition rather than one per command, so the flag cannot come to mean
#: something slightly different in one corner of the command line.
JsonOutput = Annotated[bool, typer.Option("--json")]

#: The ``--yes`` switch every destructive command carries, and it means exactly
#: one thing: **do not prompt**. Nothing else. It never skips a check.
#:
#: One definition for the same reason ``JsonOutput`` has one — and this flag is
#: why that reason is not hypothetical. Declared command by command, ``--yes``
#: drifted into three meanings: "skip the confirmation" on the `remove`
#: commands, "skip the confirmation for the dangerous subset" on `cache purge`
#: and `rollback`, and on `blitzecdn deploy` — the most destructive command in
#: the product — "skip validation, skip the check-mode preview, *and* skip the
#: confirmation". An operator putting `deploy --yes` in a CI job to answer a
#: prompt they could not answer was silently giving up two safety gates they
#: had no reason to think were attached to it. Skipping those is now
#: ``--skip-preflight``, which says so.
Yes = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Do not prompt for confirmation."),
]

#: Give up `deploy`'s pre-flight: the configuration validation and the
#: check-mode preview that runs before anything converges. Separate from
#: ``--yes`` because they are separate decisions — an unattended caller wants
#: "do not prompt" and almost never wants "and do not check either" — and named
#: for what it costs rather than for the interaction it happens to remove.
SkipPreflight = Annotated[
    bool,
    typer.Option(
        "--skip-preflight",
        help="Apply without validating configuration or previewing changes first.",
    ),
]


def emit(value: Any, *, json_output: bool, note: str | None = None) -> None:
    """Print a result, and the operator-facing note that follows it.

    ``note`` is for the sentence a human wants after the record — what changed,
    what to run next. It is suppressed under ``--json`` because that output has
    one consumer and it is not reading English. Pass only a note that is
    already computed: anything that has to *ask* the control plane belongs
    behind the caller's own ``if not json_output``.
    """
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
    if note is not None and not json_output:
        typer.echo(note)


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
