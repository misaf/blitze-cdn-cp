"""The `blitzecdn` command line, assembled from what the features contribute.

The root options and `config`, `init`, `migrate` and `setup` stay here: they
configure the control plane features load into. Loading features builds no
services, so `--help` needs no database. Importing this module assembles the
tree; ``run`` is the console-script entry point.
"""

from __future__ import annotations

from typing import cast

import typer
from pydantic import ValidationError

from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import BlitzeError, FailureKind, FeatureError, classify
from blitzecdn.core.features import CliCommandGroup, FeatureRegistry
from blitzecdn.entrypoints.cli import common, configuration, setup
from blitzecdn.entrypoints.cli.common import ExitCode, emit, services, settings
from blitzecdn.entrypoints.cli.root import app, main

app.add_typer(configuration.config_app, name="config")


def register_commands(registry: FeatureRegistry, root: typer.Typer = app) -> None:
    """Graft every contributed command group onto the root application.

    A nameless group contributes root verbs; one that `extends` joins the group
    founded under its name.
    """
    groups = registry.cli_commands()
    founders: dict[str, CliCommandGroup] = {}
    for group in groups:
        if group.name is None:
            root.registered_commands.extend(group.app.registered_commands)
        elif not group.extends:
            if group.name in founders:
                raise FeatureError(
                    f"{founders[group.name].feature} and {group.feature} both "
                    f"found the command group {group.name!r}"
                )
            founders[group.name] = group
            root.add_typer(group.app, name=group.name)
    for group in groups:
        if not group.extends:
            continue
        founder = founders.get(cast(str, group.name))
        if founder is None:
            raise FeatureError(
                f"{group.feature} extends the command group {group.name!r}, "
                "which no installed feature founds"
            )
        # Idempotent, because the groups are module-level Typer instances and
        # a second registry over the same features must not double them.
        commands = founder.app.registered_commands
        commands.extend(
            command
            for command in group.app.registered_commands
            if command not in commands
        )
    for founder in founders.values():
        if founder.order:
            _order_commands(founder)


def _order_commands(group: CliCommandGroup) -> None:
    """Put a group in its declared order, refusing one with a command unplaced."""
    registered = {
        # A command without a name is published under its function's name.
        command.name or getattr(command.callback, "__name__", "<unnamed>"): command
        for command in group.app.registered_commands
    }
    unlisted = sorted(name for name in registered if name not in group.order)
    if unlisted:
        raise FeatureError(
            f"every {group.name!r} command must be placed in the order "
            f"{group.feature} declares for it; these are not: {', '.join(unlisted)}"
        )
    group.app.registered_commands = [
        registered[name] for name in group.order if name in registered
    ]


register_commands(common.installed_features())


__all__ = [
    "ExitCode",
    "Settings",
    "app",
    "common",
    "configuration",
    "emit",
    "main",
    "register_commands",
    "run",
    "services",
    "settings",
    "setup",
]


#: How a failure reaches a script: `FailureKind` projected onto exit codes, as
#: `entrypoints.api.app` projects it onto status codes. Kept distinct so a timer
#: can tell "deployment running" from "typo". Total by construction;
#: `test_delivery_parity.py` catches a missing kind.
_EXIT_CODES: dict[FailureKind, ExitCode] = {
    FailureKind.BUSY: ExitCode.BUSY,
    FailureKind.CONFLICT: ExitCode.CONFLICT,
    FailureKind.NOT_FOUND: ExitCode.NOT_FOUND,
    FailureKind.EXECUTION: ExitCode.DEPLOYMENT_FAILED,
    FailureKind.CONFIGURATION: ExitCode.CONFIGURATION,
    # Not INVALID_INPUT: the fault is the installation, not the command line.
    FailureKind.INTERNAL: ExitCode.CONFIGURATION,
}


def _exit_code(error: BaseException) -> ExitCode:
    if isinstance(error, BlitzeError):
        return _EXIT_CODES[classify(error)]
    # Not a `BlitzeError`: the value or environment given was wrong.
    return ExitCode.INVALID_INPUT


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        # SystemExit: we are outside Click's invocation here.
        raise SystemExit(_exit_code(exc)) from exc
