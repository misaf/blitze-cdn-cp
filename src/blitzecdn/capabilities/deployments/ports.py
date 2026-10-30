from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.dns.domain import Rule
from blitzecdn.capabilities.dns.ports import ZoneEditor, ZoneStore
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.workflows.domain import Workflow, WorkflowKind
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class RuleRestore(Protocol):
    """Restore the rules wholesale, for a rollback that adopts an older release.

    Required, not optional: rules cascade-delete with their zone, which adoption
    rewrites.
    """

    def replace_all_rules(self, rules: list[Rule]) -> None: ...


class SettingRestore(Protocol):
    """Restore the fleet settings wholesale, for a rollback that adopts a release."""

    def replace_all_settings(self, settings: Mapping[str, Any]) -> None: ...


class DeploymentRequirements(Protocol):
    """The durable reasons a convergence is still owed to the fleet, by typed kind."""

    def require(self, kind: DeploymentRequirementKind) -> None: ...

    def clear(self, kind: DeploymentRequirementKind, *, through: datetime) -> None:
        """Clear ``kind`` only if it was requested at or before ``through``."""
        ...

    def pending(self, kind: DeploymentRequirementKind) -> bool: ...

    def requested_at(self, kind: DeploymentRequirementKind) -> datetime | None: ...


class DeploymentStore(Protocol):
    """Deployment history, and which release each run converged."""

    def create_deployment(
        self,
        operator: str,
        *,
        release_id: str,
        check_mode: bool,
        rollback_of: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment: ...

    def save(self, before: Deployment, after: Deployment) -> Deployment: ...

    def get_deployment(self, deployment_id: str) -> Deployment: ...

    def deployment_release(self, deployment_id: str) -> str: ...

    def paginate_deployments(
        self, *, limit: int, offset: int
    ) -> PageResult[Deployment]: ...

    def list_deployments(self, limit: int = 20) -> list[Deployment]: ...

    def latest_successful_applied(self) -> Deployment | None: ...

    def queued_deployments(self) -> list[Deployment]: ...

    def running_deployments(self) -> list[Deployment]: ...

    def prune_history(self, keep: int) -> int: ...

    def successful_rollback_target(self, current_release: str) -> Deployment: ...


class DeploymentGateway(Protocol):
    """What the certificate service needs from the deployment service.

    Whether a site is on the edges (HTTP-01 needs a served vhost), and a way to
    queue or run the convergence its material requires.
    """

    def site_is_deployed(self, site_name: str) -> bool: ...

    def deploy(
        self, operator: str, *, check: bool = False, host_limit: str | None = None
    ) -> Deployment: ...

    def converge_requirements(self, operator: str) -> Deployment | None: ...


class QueueBackgroundRunner(Protocol):
    """Enqueues a durable identifier for an out-of-process worker."""

    def enqueue(self, deployment_id: str) -> None: ...


class DeploymentLocker(Protocol):
    """The fleet-wide deployment lock, without any way to run a playbook.

    Issuance holds it so an ACME challenge never lands mid-convergence.
    """

    def lock(self) -> AbstractContextManager[Any]: ...


class DeploymentRunner(DeploymentLocker, Protocol):
    """Converges the fleet, under the lock it inherits.

    Answers only with an :class:`~blitzecdn.core.domain.runs.AnsibleRun`, never raw
    output. Other capabilities' plays are declared on their own ports.
    """

    #: ``variables`` is supplied rather than assumed so validation never writes
    #: over the desired-state file a concurrent deploy is converging.
    def validate(self, variables: Path) -> AnsibleRun: ...

    #: An empty selection runs the whole play; ``("stage",)`` stages without
    #: activating.
    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun: ...


class LogReader(Protocol):
    """Reads back a run log tail, for quoting to an operator, never for branching."""

    def __call__(self, path: Path | str | None, *, limit: int) -> str: ...


class YamlWriter(Protocol):
    """Publishes the desired-state document Ansible reads, atomically.

    Ansible reads it moments after a deploy writes it; a partial document must
    never be observable. Positional-only, so implementations may name parameters
    freely.
    """

    def __call__(self, path: Path, document: dict[str, object], /) -> None: ...


class Releases(Protocol):
    """Compiling desired state, and reading back what was compiled.

    All a deployment knows about configuration: it converges a release's artifact
    and derives nothing itself. ``compile`` records nothing, so repeated
    validation writes no history; ``prepare`` records.
    """

    def inputs(self) -> ReleaseInputs: ...

    def compile(self, *, host_limit: str | None = None) -> Release: ...

    def prepare(self, *, host_limit: str | None = None) -> Release: ...

    def get(self, release_id: str) -> Release: ...

    def inputs_for(self, release_id: str) -> ReleaseInputs: ...

    def configuration(self, release_id: str) -> ConfigurationBundle: ...


class WorkflowProgress(Protocol):
    """The checkpoint sink a convergence writes its progress to.

    ``fail`` lets a convergence that handled its errors mark the workflow failed
    without raising.
    """

    #: Both positional and required, matching the closure the coordinator builds.
    checkpoint: Callable[[str, dict[str, Any] | None], Workflow]

    def fail(self, error: str) -> None: ...


class WorkflowRun(Protocol):
    def __enter__(self) -> WorkflowProgress: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class Workflows(Protocol):
    """Opening a journal entry, and recovering the ones a restart abandoned."""

    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> WorkflowRun: ...

    def reconcile_interrupted(
        self, *, resource_id: str | None = None
    ) -> Sequence[Workflow]: ...


__all__ = [
    "DeploymentGateway",
    "DeploymentLocker",
    "DeploymentRequirements",
    "DeploymentRunner",
    "DeploymentStore",
    "DeploymentTargets",
    "EdgeAddresses",
    "EventRecorder",
    "LogReader",
    "QueueBackgroundRunner",
    "Releases",
    "RuleRestore",
    "ServingVerifier",
    "SettingRestore",
    "UnitOfWork",
    "WorkflowProgress",
    "WorkflowRun",
    "Workflows",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]


class DeploymentTargets(Protocol):
    """Per-edge rollout progress, and the fence every write to it names."""

    def claim_generation(self, deployment_id: str) -> int: ...

    def plan(self, deployment_id: str, edges: Sequence[str], *, fence: int) -> None: ...

    def paginate_targets(
        self, deployment_id: str, *, limit: int, offset: int
    ) -> PageResult[DeploymentTarget]: ...

    def targets(self, deployment_id: str) -> Sequence[DeploymentTarget]: ...

    def begin(
        self, deployment_id: str, edge: str, *, fence: int, now: datetime
    ) -> bool: ...

    def record_phase(
        self, deployment_id: str, edge: str, phase: TargetPhase, *, fence: int
    ) -> bool: ...

    def finish(
        self,
        deployment_id: str,
        edge: str,
        *,
        fence: int,
        status: TargetStatus,
        now: datetime,
        error: str | None = None,
    ) -> bool: ...

    def skip_remaining(
        self, deployment_id: str, *, fence: int, now: datetime, reason: str
    ) -> int: ...


class EdgeAddresses(Protocol):
    """Which edges a run reaches, and where each answers from outside.

    ``selected`` uses the same matcher as the Ansible ``--limit``.
    ``public_address`` is where a visitor arrives, which differs from the SSH
    address on NAT'd or multi-homed edges.
    """

    def selected(self, host_limit: str | None) -> Sequence[str]: ...

    def public_address(self, edge: str) -> str | None: ...


class ServingVerifier(Protocol):
    """Whether an edge actually answers for what it was told to serve."""

    def verify(
        self, *, edge: str, address: str, sites: Sequence[CdnSite], release_digest: str
    ) -> Sequence[Any]: ...
