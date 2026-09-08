"""What a capability contributes to a running control-plane process.

Commands, health checks, recurring jobs, and what a lifecycle hook is told
about the process it woke up in. The one group here whose members hold live
callables rather than descriptions of files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from typer import Typer

    from blitzecdn.core.config import Settings

__all__ = [
    "CliCommandGroup",
    "HealthCheck",
    "ProcessKind",
    "RuntimeContext",
    "ScheduledJob",
]


class ProcessKind(StrEnum):
    """Which long-running process is starting.

    Startup work is not the same in all of them: republishing queued
    deployments belongs to the API, which is the one process that owns the
    deployment lock for the lifetime of the node, and running it from every
    CLI invocation would take that lock a hundred times a day.
    """

    API = "api"
    CLI = "cli"
    WORKER = "worker"
    SCHEDULER = "scheduler"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """What a lifecycle contribution is told about the process it runs in."""

    process: ProcessKind
    #: The process's resolved configuration. Annotated under `TYPE_CHECKING`,
    #: the same way `Typer` is, and not left as `object`: `core.config` imports
    #: nothing from `core.plugins`, in either direction, so there is no cycle
    #: to avoid here — and an untyped member is the one place in an otherwise
    #: fully typed ABI where mypy has nothing to say about an attribute a
    #: startup hook reads off it, in this repository or in a wheel.
    settings: Settings


@dataclass(frozen=True, slots=True)
class CliCommandGroup:
    """One Typer application to graft onto the root command.

    `name` is the sub-command it appears under. `None` means the group's
    commands are root commands — `deploy`, `plan`, `rollback`, `drift` are
    verbs an operator types directly, and nesting them under a noun to satisfy
    the registration mechanism would be the mechanism dictating the interface.
    """

    #: The contributing plugin's `PluginMetadata.name`. Declared rather than
    #: inferred from the callback's `__module__`, which answers where a function
    #: was *written* and not which wheel ships it — a group whose commands
    #: delegate to a shared helper, or whose callbacks are wrapped, would be
    #: attributed to whoever defined the wrapper. A group belongs to exactly one
    #: plugin and so does every command in it, so the group is the honest place
    #: to say which.
    plugin: str
    name: str | None
    app: Typer


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One reason `/health` may answer "unavailable", and its name.

    The callable raises to fail. It returns nothing on success: a check that
    wanted to report a value would be a metric, and `/metrics` is where those
    go.
    """

    #: The contributing plugin's `PluginMetadata.name`. `name` is free-form and
    #: chosen by the plugin, so two wheels can both call a check `"database"`;
    #: without this the two are indistinguishable in `/health` and there is
    #: nothing to name in the error that refuses them.
    plugin: str
    name: str
    check: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Recurring maintenance, and the only description of it there is.

    The scheduler publishes this job's `name` to the queue and the worker
    resolves it back to `run` through the registry in its own process. That
    indirection is not a service locator: the name is a durable message
    payload crossing a process boundary, so it cannot be a function reference,
    and the table it is resolved against is this one.

    `interval_seconds` of zero disables the job, which is how every one of them
    is turned off by configuration.
    """

    #: The contributing plugin's `PluginMetadata.name`. `name` is a durable
    #: queue payload and therefore has to be unique across everything
    #: installed, which the registry enforces — but the error it raised could
    #: only quote the job name, telling an operator that two plugins collided
    #: and leaving them to work out which two.
    plugin: str
    name: str
    interval_seconds: int
    run: Callable[[str], None]
    jitter_seconds: int = 0
    #: How long the queue's single-flight key is held, so a scheduler that
    #: fires again while the previous run is still going does not stack them.
    #:
    #: Zero means twice the interval, which is the right answer for every job
    #: that has no opinion: a run that has not finished by then is stuck, not
    #: slow. A job declares a number only when it can outlast that — work
    #: bounded by its own budget rather than by its cadence.
    lease_seconds: int = 0
