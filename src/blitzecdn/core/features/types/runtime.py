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

    `name` is the sub-command it appears under; `None` makes its commands root
    verbs such as `deploy`. `extends` joins a group another feature founded
    under the same name — `domain cache` belongs to `cache` but sits under
    `domain` — and the founding group sets the help text and `order`.
    """

    #: The contributing feature's `FeatureMetadata.name`, declared rather than
    #: inferred from a callback's module, which names where it was written.
    feature: str
    name: str | None
    app: Typer
    extends: bool = False
    #: The founding group's commands by name, in `--help` order. Empty means
    #: registration order; otherwise a command missing from it is a startup
    #: failure rather than one sorted quietly to the end of the group.
    order: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.extends and self.name is None:
            raise ValueError("a group that extends another has to name it")
        if self.extends and self.order:
            raise ValueError(
                f"{self.feature} extends {self.name!r}; only the group that "
                "founded it sets its order"
            )


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One reason `/health` may answer "unavailable". The callable raises to fail."""

    #: The contributing feature's `FeatureMetadata.name`.
    feature: str
    name: str
    check: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Recurring maintenance, published by name and run by the worker.

    The name crosses a process boundary as a durable queue payload, so it is a
    name rather than a function reference, and must be unique. An
    `interval_seconds` of zero disables the job.
    """

    #: The contributing feature's `FeatureMetadata.name`.
    feature: str
    name: str
    interval_seconds: int
    run: Callable[[str], None]
    jitter_seconds: int = 0
    #: How long the queue's single-flight key is held, so runs never stack.
    #: Zero means twice the interval; a job sets it only when a run can
    #: legitimately outlast that.
    lease_seconds: int = 0
