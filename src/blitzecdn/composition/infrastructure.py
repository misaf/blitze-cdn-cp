"""The concrete adapters one control-plane process runs on.

Everything here touches the outside world or is built from what does: the
database and its stores, the Ansible runner and the roles and resources it is
handed, the probes that open sockets, and the durable job queue. Services are
built from this in :mod:`blitzecdn.composition.services` and never construct
an adapter themselves.

The keyword arguments of :meth:`~blitzecdn.composition.Infrastructure.build`
are the only test seam
in the composition: a test replaces the adapter it is about, and the service
graph built on top is the production one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.deployments.ports import (
    DeploymentRunner,
    QueueBackgroundRunner,
    ServingVerifier,
)
from blitzecdn.capabilities.edges.adapters.probe import OriginProbe
from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.capabilities.edges.ports import EdgeRunner
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.jobs.composition import (
    build_job_queue,
    build_job_scheduler,
)
from blitzecdn.capabilities.jobs.service import JobQueue, JobScheduler
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.roster import load_registry
from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import (
    PluginRegistry,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.plugins.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import PlaybookRunner

__all__ = ["FleetRunner", "Infrastructure"]


class FleetRunner(DeploymentRunner, EdgeRunner, PlaybookRunner, Protocol):
    """Every playbook capability one Ansible adapter happens to provide.

    Each capability declares the slice it actually uses — ``DeploymentRunner``,
    ``EdgeRunner``, ``DeploymentLocker`` — and none of them knows the others
    exist. That one object satisfies all of them is a fact about the adapter,
    so it is stated here, where knowing which concrete thing is wired in is the
    entire job, and nowhere else. A test that injects a fake runner is the
    other implementer.
    """


@dataclass(frozen=True, slots=True)
class Infrastructure:
    """The adapters, and the resolved contributions they were built from."""

    settings: Settings
    #: The one clock every service and store in this process reads.
    clock: Clock
    repository: Repository
    plugins: PluginRegistry
    capability_config: ResolvedCapabilityEnvironment
    edge_contributions: EdgeContributions
    edges: EdgeStorePort
    runner: FleetRunner
    origin_probe: OriginProbePort
    serving_probe: ServingVerifier
    certificate_store: CertificateStore
    job_queue: JobQueue
    job_scheduler: JobScheduler
    background: QueueBackgroundRunner
    queue_ready: Callable[[], bool]
    #: The repository this object opened, and therefore closes. An injected
    #: one stays its caller's, so two service graphs may share a store.
    _owned: list[Repository] = field(default_factory=list, repr=False)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        repository: Repository | None = None,
        plugins: PluginRegistry | None = None,
        runner: FleetRunner | None = None,
        origin_probe: OriginProbePort | None = None,
        serving_probe: ServingVerifier | None = None,
        edges: EdgeStorePort | None = None,
        background: QueueBackgroundRunner | None = None,
        queue_ready: Callable[[], bool] | None = None,
        clock: Clock = system_clock,
        pool_connections: bool = False,
    ) -> Infrastructure:
        """The real adapters, with any of them replaced by the caller's."""
        store = repository or Repository(
            settings.database_path,
            pool_connections=pool_connections,
            audit_retention=settings.audit_retention,
            clock=clock,
        )
        try:
            registry = plugins if plugins is not None else load_registry()
            # Refuse missing required capabilities before building anything
            # that would use them. See docs/decisions/0002.
            registry.require(
                settings.required_capabilities,
                subject="this installation's `required_capabilities`",
            )
            fleet = edges or store.edges
            contributions = registry.ansible_contributions()
            capability_config = resolve_capability_environment(
                registry.configuration_contributions(),
                settings.capability_environment,
                settings.capability_config_file,
                settings.state_dir,
            )
            # What the shipped features add to a run, assembled once: the
            # registry answers what is declared, and each resolver decides the
            # order and refuses what cannot work — a role two features both
            # ship, a module nothing declared.
            edge_contributions = EdgeContributions.of(
                roles_path=resolve_role_search_path(
                    settings.ansible_dir / "roles", contributions
                ),
                edge_roles=resolve_edge_capability_roles(contributions),
                host_roles=resolve_host_capability_roles(contributions),
                teardown_roles=resolve_teardown_capability_roles(contributions),
                nginx_resources=resolve_nginx_resources(registry.nginx_contributions()),
                edge_modules=resolve_edge_modules(contributions),
                environment=capability_config.environment,
            )
            job_queue = build_job_queue(
                jobs=store.jobs, retention=settings.history_retention, clock=clock
            )
            return cls(
                settings=settings,
                clock=clock,
                repository=store,
                plugins=registry,
                capability_config=capability_config,
                edge_contributions=edge_contributions,
                edges=fleet,
                # The fleet as core reads it: host names and the group they
                # form, so running a playbook makes `core.ansible` import no
                # capability.
                runner=runner
                or AnsibleRunner(settings, EdgeRoster(fleet), edge_contributions),
                origin_probe=origin_probe or OriginProbe(settings),
                serving_probe=serving_probe or ServingProbe(),
                certificate_store=CertificateStore(settings, clock=clock),
                job_queue=job_queue,
                job_scheduler=build_job_scheduler(
                    schedules=store.schedules, queue=job_queue, uow=store, clock=clock
                ),
                background=background or _QueuePublisher(job_queue),
                queue_ready=queue_ready or (lambda: _queue_reachable(job_queue)),
                _owned=[store] if repository is None else [],
            )
        except BaseException:
            if repository is None:
                store.close()
            raise

    def close(self) -> None:
        """Release what :meth:`build` opened; injected adapters stay open."""
        while self._owned:
            self._owned.pop().close()


class _QueuePublisher:
    """The deployment capability's ``QueueBackgroundRunner``, over the job queue.

    ``deployments`` declared a port with a single ``enqueue`` on it and knows
    nothing about jobs, leases or fences. Binding that port to this
    installation's queue is a composition decision, and putting it here keeps
    the deployment service unable to reach the rest of the queue's surface.
    """

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def enqueue(self, deployment_id: str) -> None:
        self._queue.enqueue_deployment(deployment_id)


def _queue_reachable(queue: JobQueue) -> bool:
    """Whether the job table answers, without changing anything in it.

    A missing table or a locked file is a node that cannot run background work;
    a `TypeError` here is a bug, and a health check that swallowed it would
    report a healthy node forever.
    """
    try:
        queue.list_jobs(limit=1)
    except (SQLAlchemyError, OSError):
        return False
    return True
