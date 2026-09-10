from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.dns.domain import CdnSite, Rule
from blitzecdn.capabilities.dns.ports import ZoneEditor, ZoneStore
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.workflows.domain import Workflow, WorkflowKind
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class RuleRestore(Protocol):
    """Putting the rules back, for a rollback that adopts an older release.

    One method, not the rule editor: a rollback replaces the table wholesale
    and never edits a rule, so the port it holds should not be able to.

    A rollback must restore them rather than leave them alone. The zone rows go
    out and back during an adoption, and a rule is keyed to its zone with ON
    DELETE CASCADE — so a rollback that ignored rules would not preserve them,
    it would delete every one.
    """

    def replace_all_rules(self, rules: list[Rule]) -> None: ...


class DeploymentRequirements(Protocol):
    """The durable reasons a convergence is still owed to the fleet.

    Typed by
    :class:`~blitzecdn.capabilities.deployments.domain.DeploymentRequirementKind`
    rather than by a bare string: the kinds are a closed set every caller has to
    agree on, and a typo in one of three call sites would otherwise raise a
    requirement nothing ever clears.
    """

    def require(self, kind: DeploymentRequirementKind) -> None: ...

    def clear(self, kind: DeploymentRequirementKind) -> None: ...

    def pending(self, kind: DeploymentRequirementKind) -> bool: ...


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

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment: ...

    def get_deployment(self, deployment_id: str) -> Deployment: ...

    def deployment_release(self, deployment_id: str) -> str: ...

    def list_deployments(self, limit: int = 20) -> list[Deployment]: ...

    def queued_deployments(self) -> list[Deployment]: ...

    def abandon_running(self) -> int: ...

    def prune_history(self, keep: int) -> int: ...

    def successful_rollback_target(self, current_release: str) -> Deployment: ...


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


class DeploymentLocker(Protocol):
    """Holds the fleet-wide "one deployment at a time" lock.

    Separate from :class:`DeploymentRunner` because certificate issuance needs
    exactly this and no way to run a playbook: it takes the lock so an ACME
    challenge cannot land halfway through a convergence, and a port that also
    offered ``run`` would let it start one.
    """

    def lock(self) -> AbstractContextManager[Any]: ...


class DeploymentRunner(DeploymentLocker, Protocol):
    """Converges the fleet, under the lock it inherits.

    Both methods answer with an :class:`~blitzecdn.core.domain.runs.AnsibleRun`, which
    is the whole of what the application layer learns about a run. There is
    deliberately no way through this port to reach the raw output.

    Narrow on purpose. One adapter runs every playbook the control plane has,
    but the purge, stats, origin-check and decommission plays are not
    deployment concerns and are declared by the capabilities that do own them —
    ``cache.ports.CacheRunner``, ``edges.ports.EdgeRunner``. Naming them all
    here made every one of those capabilities depend on this package to reach its
    own playbook, which is how the capability graph came to have cycles in it.
    """

    #: ``variables`` is supplied rather than assumed so validation never writes
    #: over the desired-state file a concurrent deploy is converging.
    def validate(self, variables: Path) -> AnsibleRun: ...

    #: ``tags`` is what makes staging separable from activating. An empty
    #: selection runs the whole play; ``("stage",)`` runs the preparation the
    #: edge play tags that way and nothing that changes what the edge serves.
    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun: ...


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


class Releases(Protocol):
    """Compiling desired state, and reading back what was compiled.

    The whole of what a deployment knows about configuration. It does not
    derive a virtual host, merge a rule, ask a plugin for a variable or render
    a document — it asks for a release and converges the artifact in it, which
    is what keeps "what should the fleet serve" answerable in one place and
    answerable after the fact.

    ``compile`` records nothing and ``prepare`` records the result; the
    distinction matters because validating is something an operator does
    repeatedly while fixing a fleet, and it should not write history.
    """

    def inputs(self) -> ReleaseInputs: ...

    def compile(self, *, host_limit: str | None = None) -> Release: ...

    def prepare(self, *, host_limit: str | None = None) -> Release: ...

    def get(self, release_id: str) -> Release: ...

    def inputs_for(self, release_id: str) -> ReleaseInputs: ...


class WorkflowProgress(Protocol):
    """The checkpoint sink a convergence writes its progress to.

    `fail` is here and `checkpoint` is not optional because this capability
    uses both: a convergence that finishes with errors it handled marks the
    workflow failed without raising, which is the one case the surrounding
    context manager cannot infer for itself.
    """

    #: Both arguments positional and neither defaulted, which is what the
    #: coordinator hands out: `checkpoint` is a closure it builds per run
    #: rather than a method, so a port that gave `details` a default would
    #: describe something no implementation offers.
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
    """Opening a journal entry, and recovering the ones a restart abandoned.

    Declared rather than imported. `convergence.py` named
    `core.application.workflows.WorkflowCoordinator` outright, which was legal
    only while the journal was core's; the moment it became a capability, a
    service importing another capability's service is the edge the layering
    rules exist to refuse. `blitzecdn-certificates` has always declared its own
    — it is on the other side of the packaging boundary and had no choice — so
    this is the shape the one out-of-tree consumer already proved.
    """

    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> WorkflowRun: ...

    def reconcile_interrupted(self) -> Sequence[Workflow]: ...


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
    "UnitOfWork",
    "WorkflowProgress",
    "WorkflowRun",
    "Workflows",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]


class DeploymentTargets(Protocol):
    """Per-edge rollout progress, and the fence every write to it names.

    ``claim_generation`` is the whole of the concurrency story. Taking a
    deployment over increments it; every subsequent write names the value the
    claim returned, so a worker that was paused past its lease updates zero
    rows and is told so rather than recording an outcome for an edge somebody
    else has since converged.
    """

    def claim_generation(self, deployment_id: str) -> int: ...

    def plan(self, deployment_id: str, edges: Sequence[str], *, fence: int) -> None: ...

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

    Two questions with one answer behind them, which is why they are one port.
    ``selected`` expands a host limit into the edges a rollout will walk, using
    the same matcher the Ansible runner uses to build its ``--limit`` — so
    "which edges does this deployment aim at" cannot have two answers.

    ``public_address`` is the address a *visitor* arrives on, not the one the
    controller connects to over SSH. They differ on any NAT'd or multi-homed
    edge, and verification is a question about the side the world sees.
    """

    def selected(self, host_limit: str | None) -> Sequence[str]: ...

    def public_address(self, edge: str) -> str | None: ...


class ServingVerifier(Protocol):
    """Whether an edge actually answers for what it was told to serve.

    Declared as a port so the rollout can be tested without a network, and
    narrow so it can never become a general HTTP client living inside a
    deployment service.
    """

    def verify(
        self, *, edge: str, address: str, sites: Sequence[CdnSite]
    ) -> Sequence[Any]: ...
