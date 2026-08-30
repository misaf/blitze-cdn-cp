"""The `blitzecdn` command line.

Importing this package is what assembles the CLI: each command module registers
its commands on the root ``app`` as a side effect of being imported, and the
sub-app registrations below are the one place the command tree is described.

``run`` is the console-script entry point (``blitzecdn = "blitzecdn.cli:run"``),
so it has to live here rather than in :mod:`blitzecdn.cli.app` — by the time it
is called every command module must already have been imported.
"""

from __future__ import annotations

import typer
from pydantic import ValidationError

from blitzecdn.cli import (
    backup,
    bootstrap,
    cache,
    certs,
    common,
    configuration,
    deploy,
    diagnostics,
    edges,
    tls,
    zones,
)
from blitzecdn.cli.app import app, main
from blitzecdn.cli.common import ExitCode, control_plane, emit, settings
from blitzecdn.config import Settings
from blitzecdn.exceptions import (
    BlitzeError,
    ConfigurationError,
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)

app.add_typer(zones.site_app, name="site")
app.add_typer(edges.edge_app, name="edge")
app.add_typer(zones.domain_app, name="domain")
app.add_typer(zones.record_app, name="record")
app.add_typer(zones.dns_app, name="dns")
app.add_typer(certs.cert_app, name="cert")
app.add_typer(edges.origin_app, name="origin")
app.add_typer(cache.cache_app, name="cache")
app.add_typer(tls.ssl_app, name="ssl")
app.add_typer(configuration.config_app, name="config")
app.add_typer(backup.backup_app, name="backup")

__all__ = [
    "ExitCode",
    "Settings",
    "app",
    "backup",
    "bootstrap",
    "cache",
    "certs",
    "common",
    "configuration",
    "control_plane",
    "deploy",
    "diagnostics",
    "edges",
    "emit",
    "main",
    "run",
    "settings",
    "tls",
    "zones",
]


#: How a failure reaches a script, mirroring the API's status mapping.
#:
#: The HTTP layer separates these deliberately — a conflict is not a bad
#: request, and a dependency that misbehaved is not a controller that is down —
#: and a caller driving the CLI needs the same distinction for the same reason.
#: Every one of these used to exit `2`, so a systemd timer could not tell "a
#: deployment is already running, come back shortly" from "you typed the site
#: name wrong", and both looked like a usage error.
#:
#: Walked most-specific first, because `DeploymentBusyError` is a
#: `ConflictError` and would otherwise be matched by its parent.
_EXIT_CODES: tuple[tuple[type[BlitzeError], ExitCode], ...] = (
    (DeploymentBusyError, ExitCode.BUSY),
    (ConflictError, ExitCode.CONFLICT),
    (NotFoundError, ExitCode.NOT_FOUND),
    (ExecutionError, ExitCode.DEPLOYMENT_FAILED),
    (ConfigurationError, ExitCode.CONFIGURATION),
)


def _exit_code(error: BaseException) -> ExitCode:
    for kind, code in _EXIT_CODES:
        if isinstance(error, kind):
            return code
    # A `BlitzeError` with no mapping above, or a ValidationError/OSError:
    # the input or the environment was wrong in a way no command anticipated.
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
