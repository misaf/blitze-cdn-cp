from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.runs import AnsibleRun
from blitzecdn.features.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.features.dns.ports import ZoneEditor, ZoneStore

if TYPE_CHECKING:
    from blitzecdn.features.cache.domain import PurgeEntry


class DeploymentRequirements(Protocol):
    """The durable reasons a convergence is still owed to the fleet.

    Typed by :class:`~blitzecdn.features.deployments.domain.DeploymentRequirementKind`
    rather than by a bare string: the kinds are a closed set every caller has to
    agree on, and a typo in one of three call sites would otherwise raise a
    requirement nothing ever clears.
    """

    def require(self, kind: DeploymentRequirementKind) -> None: ...

    def clear(self, kind: DeploymentRequirementKind) -> None: ...

    def pending(self, kind: DeploymentRequirementKind) -> bool: ...


class DeploymentStore(Protocol):
    """Deployment history and the snapshots it converges."""

    def snapshot(self) -> str: ...

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment: ...

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment: ...

    def get_deployment(self, deployment_id: str) -> Deployment: ...

    def deployment_snapshot(self, deployment_id: str) -> str: ...

    def list_deployments(self, limit: int = 20) -> list[Deployment]: ...

    def queued_deployments(self) -> list[Deployment]: ...

    def abandon_running(self) -> int: ...

    def prune_history(self, keep: int) -> int: ...

    def successful_rollback_target(self, current_snapshot: str) -> Deployment: ...


class DeploymentGateway(Protocol):
    """What the certificate service needs from the deployment service.

    Issuance asks whether a site is actually on the edges (HTTP-01 cannot
    validate a vhost nothing serves), and reconciliation installs what it
    issued. Nothing else.
    """

    def site_is_deployed(self, site_name: str) -> bool: ...

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment: ...


class QueueBackgroundRunner(Protocol):
    """Enqueues a durable identifier for an out-of-process worker."""

    def enqueue(self, deployment_id: str) -> None: ...


class DeploymentRunner(Protocol):
    """Runs Ansible and owns the cross-process deployment lock.

    Every method answers with an :class:`~blitzecdn.core.runs.AnsibleRun`,
    which is the whole of what the application layer learns about a run. There
    is deliberately no way through this port to reach the raw output.
    """

    def lock(self) -> AbstractContextManager[Any]: ...

    #: ``variables`` is supplied rather than assumed so validation never writes
    #: over the desired-state file a concurrent deploy is converging.
    def validate(self, variables: Path) -> AnsibleRun: ...

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun: ...

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun: ...

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun: ...

    #: ``sites`` is passed rather than read from the desired-state file: this
    #: takes no deployment lock, so that file may belong to a deploy in flight.
    def run_origin_check(
        self, *, sites: list[dict[str, object]], host_limit: str | None = None
    ) -> AnsibleRun: ...

    def run_decommission(self, *, host_limit: str) -> AnsibleRun: ...


class LogReader(Protocol):
    """Reads back a run log, for showing an operator what Ansible said.

    Narrow on purpose. Application code may quote a log into a message; it may
    not branch on one, and a port with a single tail-reading method is what
    keeps that distinction enforceable rather than merely intended.
    """

    def __call__(self, path: Path | str | None, *, limit: int) -> str: ...


class YamlWriter(Protocol):
    """Publishes the desired-state document Ansible reads.

    A port rather than a bare ``Callable`` so it reads like its neighbours and
    can carry the one thing about it that matters: the write has to be atomic.
    A deploy renders this file while holding the deployment lock and Ansible
    reads it moments later, so a reader must never observe a partial document —
    an edge converged from half a desired state is worse than one that did not
    converge at all.

    Positional-only, because the parameters of a function-shaped port are an
    implementation's business: the adapter may name its second argument
    ``payload`` and take extra keyword arguments with defaults, and none of
    that is something a caller here should have to match.
    """

    def __call__(self, path: Path, document: dict[str, object], /) -> None: ...


class DesiredStateRenderer(Protocol):
    def render(self, snapshot: str, path: Path) -> None: ...


__all__ = [
    "DeploymentGateway",
    "DeploymentRequirements",
    "DeploymentRunner",
    "DeploymentStore",
    "DesiredStateRenderer",
    "EventRecorder",
    "LogReader",
    "QueueBackgroundRunner",
    "UnitOfWork",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]
