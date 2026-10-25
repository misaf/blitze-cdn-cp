"""What a capability contributes to a running control-plane process.

Commands, health checks and recurring jobs. The one group here whose members
hold live callables rather than descriptions of files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from typer import Typer

__all__ = [
    "CliCommandGroup",
    "HealthCheck",
    "ScheduledJob",
]


@dataclass(frozen=True, slots=True)
class CliCommandGroup:
    """One Typer application to graft onto the root command.

    `name` is the sub-command it appears under. `None` means the group's
    commands are root commands — `deploy`, `plan`, `rollback`, `drift` are
    verbs an operator types directly, and nesting them under a noun to satisfy
    the registration mechanism would be the mechanism dictating the interface.
    """

    #: The contributing feature's `FeatureMetadata.name`. Declared rather than
    #: inferred from the callback's `__module__`, which answers where a function
    #: was *written* and not which feature ships it — a group whose commands
    #: delegate to a shared helper, or whose callbacks are wrapped, would be
    #: attributed to whoever defined the wrapper. A group belongs to exactly one
    #: feature and so does every command in it, so the group is the honest place
    #: to say which.
    feature: str
    name: str | None
    app: Typer


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One reason `/health` may answer "unavailable", and its name.

    The callable raises to fail. It returns nothing on success: a check that
    wanted to report a value would be a metric, and `/metrics` is where those
    go.
    """

    #: The contributing feature's `FeatureMetadata.name`. `name` is free-form and
    #: chosen by the feature, so two wheels can both call a check `"database"`;
    #: without this the two are indistinguishable in `/health` and there is
    #: nothing to name in the error that refuses them.
    feature: str
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

    #: The contributing feature's `FeatureMetadata.name`. `name` is a durable
    #: queue payload and therefore has to be unique across everything
    #: installed, which the registry enforces — but the error it raised could
    #: only quote the job name, telling an operator that two features collided
    #: and leaving them to work out which two.
    feature: str
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
