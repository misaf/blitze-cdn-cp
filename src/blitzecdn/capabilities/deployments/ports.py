from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.capabilities.dns.ports import ZoneEditor, ZoneStore
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.plugins import StateValue, ValidationResult
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class SiteRestore(Protocol):
    """Putting the sites back, for a rollback that adopts an older snapshot.

    One method, and deliberately not ``sites.ports.SiteStore``: a rollback
    replaces the table wholesale and never edits one site, so the port it holds
    should not be able to. The write side of a *single* site belongs to
    ``SiteService`` and this capability has no business with it.
    """

    def replace_all_sites(self, sites: list[CdnSite]) -> None: ...


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

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun: ...


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


class StateContributors(Protocol):
    """Every plugin's share of a desired-state document, already merged.

    Declared here rather than imported from the plugin registry for the
    ordinary reason a port is declared by its consumer: the renderer needs two
    mappings, not a plugin manager. It also keeps this capability testable with a
    hand-built pair of dictionaries and no plugins registered anywhere.
    """

    def site_variables(self, site: CdnSite) -> Mapping[str, StateValue]: ...

    def fleet_variables(
        self, sites: tuple[CdnSite, ...]
    ) -> Mapping[str, StateValue]: ...


class SiteValidator(Protocol):
    """What the installed plugins know that should stop a deployment.

    Asked once per site before anything is rendered, so refusing costs nothing:
    no desired-state file is written and no playbook starts.
    """

    def validate_site(self, site: CdnSite) -> ValidationResult: ...


__all__ = [
    "DeploymentGateway",
    "DeploymentLocker",
    "DeploymentRequirements",
    "DeploymentRunner",
    "DeploymentStore",
    "DesiredStateRenderer",
    "EventRecorder",
    "LogReader",
    "QueueBackgroundRunner",
    "SiteRestore",
    "SiteValidator",
    "StateContributors",
    "UnitOfWork",
    "YamlWriter",
    "ZoneEditor",
    "ZoneStore",
]
