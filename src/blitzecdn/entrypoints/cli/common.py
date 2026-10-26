"""Helpers every command group shares; patch ``common.container`` in one place."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextvars import ContextVar
from enum import IntEnum
from typing import Annotated, Any, overload

import typer
import yaml
from dishka import Container

from blitzecdn.composition import cli_container, load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.features import FeatureRegistry


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    CONFIGURATION = 3
    CONFLICT = 4
    DEPLOYMENT_FAILED = 5
    #: Drift found, or an edge unreachable; distinct from a broken check.
    DRIFT_DETECTED = 6
    #: The thing asked about does not exist. The CLI's 404.
    NOT_FOUND = 7
    #: Another deployment is running: the one failure to retry, not alert on.
    BUSY = 8


def settings() -> Settings:
    return Settings.from_environment()


#: The container the current invocation has built, once it has asked for it.
#: ``None`` outside an invocation scope.
_invocation: ContextVar[list[Container] | None] = ContextVar(
    "blitzecdn_cli_container", default=None
)


def scope_services(context: typer.Context) -> None:
    """Hold one invocation's container until the command line exits, then close it."""
    held: list[Container] = []
    _invocation.set(held)

    def release() -> None:
        _invocation.set(None)
        while held:
            held.pop().close()

    context.call_on_close(release)


def container() -> Container:
    """What commands resolve from: built once per invocation, or afresh outside one."""
    held = _invocation.get()
    if held is None:
        return cli_container(settings())
    if not held:
        held.append(cli_container(settings()))
    return held[0]


@overload
def service[T](kind: type[T]) -> T: ...
@overload
def service(kind: Any) -> Any: ...
def service(kind: Any) -> Any:
    """One service from this invocation's container.

    Typed as dishka's ``Container.get``: a Protocol resolves, untyped.
    """
    return container().get(kind)


def installed_features() -> FeatureRegistry:
    """The shipped feature registry, for commands that need no database.

    `blitzecdn ansible roles-path` and `blitzecdn edge image spec` run in lint
    steps and image builds.
    """
    return load_registry()


#: The one ``--json`` definition every command shares.
JsonOutput = Annotated[bool, typer.Option("--json")]

#: The ``--yes`` switch means only "do not prompt"; it never skips a check.
#: That is ``--skip-preflight``.
Yes = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Do not prompt for confirmation."),
]

#: Skip `deploy`'s validation and check-mode preview; separate from ``--yes``.
SkipPreflight = Annotated[
    bool,
    typer.Option(
        "--skip-preflight",
        help="Apply without validating configuration or previewing changes first.",
    ),
]


def emit(value: Any, *, json_output: bool, note: str | None = None) -> None:
    """Print a result, then an operator-facing ``note`` unless ``--json``.

    Pass only a computed note; anything that queries belongs behind the caller's
    own ``if not json_output``.
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
        # safe_dump renders every model, list or dict passed here.
        typer.echo(yaml.safe_dump(value, sort_keys=False).rstrip())
    if note is not None and not json_output:
        typer.echo(note)


def describe_hosts(hosts: Sequence[HostRun]) -> str:
    """Render per-host results: only the tasks that changed or failed."""
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
