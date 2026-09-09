"""The `blitzecdn` command line.

The command tree is assembled from what the installed plugins contribute, not
from a list of imports here. A capability's commands appear because its plugin
returns a :class:`~blitzecdn.core.plugins.CliCommandGroup`, so a separately
installed package adds `blitzecdn waf ...` without this module changing.

Two things stay here because they are the command line itself rather than a
capability: the root callback in :mod:`blitzecdn.cli.root` with the global
options, and `config`, `init` and `setup` — in :mod:`blitzecdn.cli.setup` and
:mod:`blitzecdn.cli.configuration` — which configure the control plane that
capabilities are then loaded into.

Discovery does not build a control plane. The tree has to exist before an
argument is parsed, and a command resolves its services when it runs — otherwise
`blitzecdn --help` would create and migrate the database.

Importing this module assembles the command tree, exactly as it did when the
tree was a list of imports: discovery is cheap, and a `blitzecdn.cli.main` that
had been imported but had no commands on it would be a trap for every caller
that reaches for `app` — the console script, the tests, and `--help`.

``run`` is the console-script entry point.
"""

from __future__ import annotations

import typer
from pydantic import ValidationError

from blitzecdn.cli import common, configuration, setup
from blitzecdn.cli.common import ExitCode, control_plane, emit, settings
from blitzecdn.cli.root import app, main
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import BlitzeError, FailureKind, classify
from blitzecdn.core.plugins import PluginRegistry

app.add_typer(configuration.config_app, name="config")


def register_commands(registry: PluginRegistry, root: typer.Typer = app) -> None:
    """Graft every contributed command group onto the root application.

    A group with no name contributes root-level verbs — `deploy`, `plan`,
    `status` — by handing over its commands rather than nesting them under a
    noun invented to satisfy the registration mechanism.
    """
    for group in registry.cli_commands():
        if group.name is None:
            root.registered_commands.extend(group.app.registered_commands)
        else:
            root.add_typer(group.app, name=group.name)


register_commands(common.installed_plugins())


__all__ = [
    "ExitCode",
    "Settings",
    "app",
    "common",
    "configuration",
    "control_plane",
    "emit",
    "main",
    "register_commands",
    "run",
    "settings",
    "setup",
]


#: How a failure reaches a script. The projection of core's `FailureKind` onto
#: this delivery layer; `blitzecdn.api.app` projects the same kinds onto status
#: codes, and neither table classifies exceptions itself any more.
#:
#: The distinctions are kept apart deliberately — a conflict is not a bad
#: request, and a dependency that misbehaved is not a controller that is down.
#: Collapsed onto one code, a systemd timer cannot tell "a deployment is
#: already running, come back shortly" from "you typed the site name wrong",
#: and both look like a usage error.
#:
#: Total over `FailureKind` by construction: `_exit_code` subscripts it, so a
#: kind added to core without a line here is a `KeyError` the parity test in
#: `tests/contract/test_delivery_parity.py` raises before a release does.
_EXIT_CODES: dict[FailureKind, ExitCode] = {
    FailureKind.BUSY: ExitCode.BUSY,
    FailureKind.CONFLICT: ExitCode.CONFLICT,
    FailureKind.NOT_FOUND: ExitCode.NOT_FOUND,
    FailureKind.EXECUTION: ExitCode.DEPLOYMENT_FAILED,
    FailureKind.CONFIGURATION: ExitCode.CONFIGURATION,
    # Not INVALID_INPUT. A plugin that will not load is not a mistyped
    # argument, and telling a script it was one sends the operator to their
    # command line instead of to their installation.
    FailureKind.INTERNAL: ExitCode.CONFIGURATION,
}


def _exit_code(error: BaseException) -> ExitCode:
    if isinstance(error, BlitzeError):
        return _EXIT_CODES[classify(error)]
    # A ValidationError or OSError: not part of core's taxonomy because neither
    # is a `BlitzeError`. The value handed in, or the environment it named, was
    # wrong in a way no command anticipated — which *is* the operator's input.
    return ExitCode.INVALID_INPUT


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        # SystemExit, not typer.Exit: we are outside Click's invocation by the
        # time app() has raised, so a typer.Exit here is nothing but an
        # unhandled exception and prints a traceback over the message above.
        raise SystemExit(_exit_code(exc)) from exc
