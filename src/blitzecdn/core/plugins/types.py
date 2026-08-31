"""The values plugins hand back, and the identity they hand back with them.

Everything here is a frozen dataclass rather than a dictionary. A contribution
crosses a package boundary — often one shipped as a separate distribution — so
"which keys does this have" has to be answerable by reading a type rather than
by reading whichever plugin happened to produce the value.

Nothing in this module imports a feature. `core` is what a feature builds on;
the two runtime-bound hooks that need built services take the composition root
as an argument instead, which keeps the arrow pointing one way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from typer import Typer

#: The pluggy project name. It prefixes every hook, which is what lets one
#: manager host hooks from packages that never heard of each other.
PROJECT_NAME = "blitzecdn"

#: The entry-point group an external distribution advertises itself in.
ENTRY_POINT_GROUP = "blitzecdn.plugins"

#: The hook-contract version a plugin is written against. Bumped only when a
#: hookspec changes shape in a way an existing implementation cannot survive;
#: a plugin declaring a different major is refused with its name rather than
#: failing later inside a hook call nobody can attribute.
HOOK_API_VERSION = 1

#: What a contribution is allowed to be. Ansible variables are YAML, so this is
#: what `yaml.safe_dump` will accept and what the edge roles can read back.
type StateValue = (
    str
    | int
    | float
    | bool
    | Mapping[str, "StateValue"]
    | tuple["StateValue", ...]
    | list["StateValue"]
    | None
)


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Who a plugin is, and what happens when it cannot be loaded.

    `required` is the whole of the failure policy. A required plugin that
    fails to import or register stops the process at startup, because the
    control plane without it is not a degraded control plane but a wrong one —
    a `dns` that did not load would silently render an empty fleet. An optional
    plugin's failure is reported with its name and the process continues.
    """

    name: str
    version: str
    required: bool = False
    api_version: int = HOOK_API_VERSION
    summary: str = ""
    #: The capability tokens this plugin supplies, for configuration that has
    #: to say what it depends on. Empty means "just my own name", which is the
    #: right answer for almost every plugin: `backup` provides `backup`.
    #:
    #: It is separate from `name` because the two answer different questions.
    #: `name` identifies the *plugin* — it is how a duplicate is detected and a
    #: failure attributed — while a token names a *capability*, which is what a
    #: site or a fleet setting actually depends on. One plugin may supply
    #: several, and a replacement implementation may supply a token another
    #: package used to, under its own name.
    provides: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"plugin name {self.name!r} must be alphanumeric")
        for token in self.provides:
            if not token or not token.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"capability {token!r} must be alphanumeric")

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token this plugin answers for, its name included."""
        return self.provides | {self.name}


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
    #: `Settings`, untyped here only because `core.plugins` sits underneath
    #: `core.config` in no particular order and the value is opaque to this
    #: module. Every implementation annotates it concretely.
    settings: object


@dataclass(frozen=True, slots=True)
class CliCommandGroup:
    """One Typer application to graft onto the root command.

    `name` is the sub-command it appears under. `None` means the group's
    commands are root commands — `deploy`, `plan`, `rollback`, `drift` are
    verbs an operator types directly, and nesting them under a noun to satisfy
    the registration mechanism would be the mechanism dictating the interface.
    """

    name: str | None
    app: Typer


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One reason `/health` may answer "unavailable", and its name.

    The callable raises to fail. It returns nothing on success: a check that
    wanted to report a value would be a metric, and `/metrics` is where those
    go.
    """

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

    name: str
    interval_seconds: int
    run: Callable[[str], None]
    jitter_seconds: int = 0
    #: How long the queue's single-flight key is held, so a scheduler that
    #: fires again while the previous run is still going does not stack them.
    lease_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class SiteStateContribution:
    """One plugin's share of the Ansible document for one virtual host.

    `overrides` is what makes merging order-independent. Two plugins writing
    the same variable is a conflict unless exactly one of them says it is
    replacing a value another produced — `certificates` deliberately replaces
    the certificate paths projected from the site model, and saying so is the
    difference between a designed override and whichever plugin loaded last.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FleetStateContribution:
    """Variables about the fleet as a whole rather than about one site.

    Separate from `SiteStateContribution` because it is derived from every
    site at once: which single site carries `reuseport` on the QUIC listener
    is not a fact any one site knows about itself.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Something a plugin knows about a site that a deployment should hear.

    A blocking issue refuses the deployment before anything is rendered; a
    warning is reported and converged anyway.
    """

    plugin: str
    site: str
    message: str
    severity: Severity = Severity.BLOCKING


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Every plugin's answer about one site, and whether it may be deployed."""

    site: str
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is Severity.BLOCKING
        )

    @property
    def ok(self) -> bool:
        return not self.blocking


__all__ = [
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "CliCommandGroup",
    "FleetStateContribution",
    "HealthCheck",
    "PluginMetadata",
    "ProcessKind",
    "RuntimeContext",
    "ScheduledJob",
    "Severity",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
]
