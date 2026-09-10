"""The production composition root.

Construction proceeds through adapters, services, plugins, and contributions.
Plugin discovery runs first so contributed Ansible roles are available when
the runner is constructed. Plugins receive the assembled control plane.

Capability-local builders assemble services from explicitly injected ports.
Entry layers call these services directly; the concrete repository is not
published. Optional packages use the published services and ports.

Durable work goes through the ``jobs`` capability, which is rows in this
control plane's own database rather than a broker. The worker is an entry point
and must not be imported here. Architecture tests enforce this boundary.
See docs/decisions/0001-zone-policy-and-composition.md for the design rationale,
and 0008 for why background work lives on the primary database."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.deployments.composition import build_deployment_service
from blitzecdn.capabilities.deployments.ports import (
    DeploymentLocker,
    DeploymentRequirements,
    DeploymentRunner,
    QueueBackgroundRunner,
    ServingVerifier,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.composition import (
    build_dns_service,
    build_host_service,
    build_rule_service,
)
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import HostService, RuleService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.adapters.probe import OriginProbe
from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.capabilities.edges.composition import build_edge_operations_service
from blitzecdn.capabilities.edges.ports import EdgeRunner
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.jobs.composition import (
    build_handlers,
    build_job_queue,
    build_job_scheduler,
)
from blitzecdn.capabilities.jobs.service import JobQueue, JobRunner, JobScheduler
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.maintenance.composition import build_maintenance_service
from blitzecdn.capabilities.releases.composition import build_release_service
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.composition.repository import Repository
from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import (
    HealthCheck,
    PluginRegistry,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
    load_plugins,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.plugins.types import ENTRY_POINT_GROUP
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import AuditTrail, PlaybookRunner

#: Built-in plugins in stable registration and presentation order.
#: The roster belongs to composition; core discovery accepts arbitrary plugins.
#: Desired-state merging is order-independent.
BUILTIN_PLUGINS: tuple[str, ...] = (
    # The capability contracts first: nothing they contribute depends on
    # another capability being registered, and `dns` composes their policy.
    "blitzecdn.capabilities.http.plugin",
    "blitzecdn.capabilities.dns.plugin",
    "blitzecdn.capabilities.edges.plugin",
    "blitzecdn.capabilities.jobs.plugin",
    "blitzecdn.capabilities.releases.plugin",
    "blitzecdn.capabilities.workflows.plugin",
    "blitzecdn.capabilities.deployments.plugin",
    "blitzecdn.capabilities.tls.plugin",
    "blitzecdn.capabilities.maintenance.plugin",
    "blitzecdn.capabilities.diagnostics.plugin",
)


def load_control_plane_plugins(
    *,
    entry_point_group: str | None = ENTRY_POINT_GROUP,
) -> PluginRegistry:
    """The roster above, loaded by core's mechanism. The call everyone wants.

    `core.plugins.load_plugins` registers whatever module paths it is given and
    knows no capability by name. This pairs it with what this distribution
    actually ships, so a caller asking "what is installed here" — the API
    building its routers, the CLI building its command tree, `blitzecdn ansible
    search-path` in an image build — asks once and asks in one place.

    ``entry_point_group=None`` skips external discovery, which is what a test
    asserting on the built-in set wants: its answer should not change because a
    developer happens to have an unrelated BlitzeCDN plugin in the same
    virtualenv.
    """
    return load_plugins(BUILTIN_PLUGINS, entry_point_group=entry_point_group)


class FleetRunner(DeploymentRunner, EdgeRunner, PlaybookRunner, Protocol):
    """Every playbook capability one Ansible adapter happens to provide.

    Each capability declares the slice it actually uses — ``DeploymentRunner``,
    ``EdgeRunner``, ``DeploymentLocker`` — and none of them knows the others
    exist. That one object satisfies all of them is a fact about the adapter,
    so it is stated here, where knowing which concrete thing is wired in is the
    entire job, and nowhere else. A test that injects a fake runner is the
    other implementer.

    ``PlaybookRunner`` is the odd one out and deliberately so. It is not a
    capability's port but core's own, published as ``ControlPlane.fleet``, and it
    is what an *installed* capability is handed: the generic "run this play"
    and nothing capability-shaped. A detachable package declares its own narrow
    port over it — ``blitzecdn_cache.ports.CacheRunner`` — which is why no
    ``CacheRunner`` appears in this list. That is the difference between a
    built-in, whose port core may name, and a distribution core has never heard
    of.
    """


class ControlPlane:
    """The wired services, and the ports the entry layers read through."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository | None = None,
        runner: FleetRunner | None = None,
        origin_probe: OriginProbePort | None = None,
        serving_probe: ServingVerifier | None = None,
        edges_store: EdgeStorePort | None = None,
        background: QueueBackgroundRunner | None = None,
        queue_ready: Callable[[], bool] | None = None,
        pool_connections: bool = False,
        plugins: PluginRegistry | None = None,
        process: ProcessKind = ProcessKind.CLI,
    ) -> None:
        self.settings = settings
        #: Which process this control plane is being built for. Lifecycle
        #: contributions branch on it; nothing else does.
        self.process = process
        store = repository or Repository(
            settings.database_path,
            pool_connections=pool_connections,
            audit_retention=settings.audit_retention,
        )
        self._owned_repository = store if repository is None else None
        # Discover package roles before constructing the Ansible runner.
        # Plugins receive the control plane after its services are wired.
        self.plugins = plugins if plugins is not None else load_control_plane_plugins()
        # Refuse missing required capabilities before wiring services.
        # See docs/decisions/0002-capability-configuration-ownership.md.
        self.plugins.require(
            self.settings.required_capabilities,
            subject="this installation's `required_capabilities`",
        )
        self._wire_adapters(
            store=store,
            runner=runner,
            origin_probe=origin_probe,
            serving_probe=serving_probe,
            edges_store=edges_store,
            background=background,
            queue_ready=queue_ready,
        )
        self._jobs: dict[str, ScheduledJob] | None = None
        self._job_runner: JobRunner | None = None
        self._wire_services(store)

    def _wire_adapters(
        self,
        *,
        store: Repository,
        runner: FleetRunner | None,
        origin_probe: OriginProbePort | None,
        serving_probe: ServingVerifier | None,
        edges_store: EdgeStorePort | None,
        background: QueueBackgroundRunner | None,
        queue_ready: Callable[[], bool] | None,
    ) -> None:
        """Choose concrete outside-world capabilities and their test overrides."""
        # The fleet, and the rows the `blitzecdn` Ansible inventory plugin reads
        # for itself at the start of every run. Both the runner and preflight
        # take it so that "which edges exist" has exactly one answer, whoever is
        # asking and whichever process they are in.
        self._edges_store = edges_store or store.edges
        self.edge_inventory: EdgeStorePort = self._edges_store
        self.ansible_settings = store.ansible_settings
        contributions = self.plugins.ansible_contributions()
        nginx_resources = resolve_nginx_resources(self.plugins.nginx_contributions())
        edge_modules = resolve_edge_modules(contributions)
        # Resolve package-owned configuration once: scoped for the controller,
        # with separately declared forwarding into Ansible.
        self.capability_config = resolve_capability_environment(
            self.plugins.configuration_contributions(),
            self.settings.capability_environment,
            self.settings.capability_config_file,
            self.settings.state_dir,
        )
        # What the installed capabilities add to a run, assembled once. This is
        # the one place that knows both halves: the registry answers what is
        # installed, and each resolver decides the order and refuses what
        # cannot work — a role two packages both ship, a module nothing
        # declared. Detaching a package empties its share of this with nothing
        # in core edited.
        edge_contributions = EdgeContributions.of(
            roles_path=resolve_role_search_path(
                self.settings.ansible_dir / "roles",
                contributions,
            ),
            # Which of those roles core's own plays run, in each of the three
            # slots: two in the edge play, one in the decommission play. A
            # package that ships a role only its own plays reach declares the
            # directory and no slot at all.
            edge_roles=resolve_edge_capability_roles(contributions),
            host_roles=resolve_host_capability_roles(contributions),
            teardown_roles=resolve_teardown_capability_roles(contributions),
            nginx_resources=nginx_resources,
            # And the dynamic modules those resources need loaded. The same
            # question one level down: a contributed `brotli` directive is a
            # syntax error on an edge that never loaded the module, and an
            # edge that loads one no installed capability asked for is the
            # image enumerating capabilities instead of the controller.
            edge_modules=edge_modules,
            environment=self.capability_config.environment,
        )
        self._runner = runner or AnsibleRunner(
            self.settings,
            # The fleet as core reads it: host names and the group they form.
            # Core declares that port and `edges` satisfies it, so running a
            # playbook does not make `core.ansible` import a capability.
            EdgeRoster(self._edges_store),
            edge_contributions,
        )
        self._origin_probe = origin_probe or OriginProbe(self.settings)
        self.origin_probe: OriginProbePort = self._origin_probe
        # What a deployment's final phase asks the fleet. Injectable for the
        # same reason the origin probe is: it opens a socket to somewhere the
        # operator named, and a test of anything else must be able to say what
        # that socket answers without one being opened.
        self._serving_probe: ServingVerifier = serving_probe or ServingProbe()
        self.deployment_lock: DeploymentLocker = self._runner
        # The durable queue, on the same database as everything else. Built
        # here rather than in `_wire_services` because `deployments` is handed
        # it as a collaborator, and because the API process needs it to publish
        # without ever consuming — which is why the handler table is empty
        # unless this is the worker.
        self.job_queue: JobQueue = build_job_queue(
            self,
            jobs=store.jobs,
            handlers=build_handlers(self) if self.process is ProcessKind.WORKER else {},
        )
        self.job_scheduler: JobScheduler = build_job_scheduler(
            self, schedules=store.schedules, queue=self.job_queue
        )
        self._background = background or _QueuePublisher(self.job_queue)
        self._queue_ready: Callable[[], bool] = queue_ready or (
            lambda: _queue_reachable(self.job_queue)
        )

    def _wire_services(self, store: Repository) -> None:
        """Build cross-cutting services, then capability-oriented services."""

        # Entry layers receive only the read side of the audit trail, so they
        # cannot manufacture an event for an action no service performed.
        self.audit: AuditTrail = store.audit_log

        # Packages read derived hosts through SiteReader and run plays through
        # PlaybookRunner. DNS supplies the host projection after it is built.
        self.sites: SiteReader
        self.fleet: PlaybookRunner = self._runner
        self.transactions: UnitOfWork = store
        self.deployment_requirements: DeploymentRequirements = (
            store.deployment_requirements
        )

        # The same audit adapter is exposed read-only to entry layers and as an
        # event recorder to services. There is one durable consumer, so a
        # generic observer registry would only disguise this ownership.
        self.events = store.audit_log
        self.workflows = WorkflowCoordinator(
            journal=store.workflows,
            uow=store,
            retention=self.settings.history_retention,
        )
        self.workflow_history = store.workflows

        # Each store is passed where its port is asked for, so a service is
        # handed the slice of persistence it declared and no more.
        # DNS owns canonical zones, rules, records, and their host projection.
        self.dns: DnsService = build_dns_service(
            self, zones=store.zones, rules=store.rules
        )
        self.site_editor: HostService = build_host_service(
            self, zones=store.zones, rules=store.rules
        )
        self.sites = self.site_editor
        # The overrides on a zone's policy, and the resolver over the two. The
        # zone store arrives through `ZoneReader`, which is one read: a rule
        # may consult the policy it overrides and may never write one.
        self.rules: RuleService = build_rule_service(
            self, rules=store.rules, zones=store.zones
        )
        # Built before the capabilities that consume it, and after `dns`: a
        # release is compiled from canonical state, and every plugin that
        # contributes a variable to one has by now been registered.
        self.releases: ReleaseService = build_release_service(
            self, state=store, releases=store.releases, edges=self._edges_store
        )
        self._wire_capability_services(store)

    def _wire_capability_services(self, store: Repository) -> None:
        """Hand each remaining capability the slice of persistence it declared.

        One call per capability, and each one names only what this root had to
        decide: which store, which runner. How a capability puts those together
        is its own `composition.py`, beside the service it builds — so a
        collaborator added to `DeploymentService` is a change to `deployments`
        and not to the file that wires the whole control plane.
        """
        self.deployments: DeploymentService = build_deployment_service(
            self,
            deployments=store.deployments,
            zones=store.zones,
            rules=store.rules,
            requirements=store.deployment_requirements,
            releases=self.releases,
            targets=store.deployment_targets,
            edges=self._edges_store,
            verifier=self._serving_probe,
            runner=self._runner,
            background=self._background,
        )
        self.edges: EdgeOperationsService = build_edge_operations_service(
            self, edges=self._edges_store, runner=self._runner
        )
        self.maintenance: MaintenanceService = build_maintenance_service(
            self, requirements=store.deployment_requirements
        )

    @property
    def job_runner(self) -> JobRunner:
        """The loop the worker process runs, built on first use.

        On first use and not in the constructor, for the same reason ``jobs``
        is: it closes over the handler table, which reaches two services this
        object is still in the middle of building. Kept once it is built, so
        two calls do not hand out two loops over one queue.
        """
        if self._job_runner is None:
            self._job_runner = JobRunner(
                queue=self.job_queue,
                scheduler=self.job_scheduler,
                poll_seconds=self.settings.worker_poll_seconds,
            )
        return self._job_runner

    @property
    def jobs(self) -> dict[str, ScheduledJob]:
        """Every scheduled job the installed plugins contribute, by name.

        Resolved on first use rather than in the constructor, because a plugin
        contributing a job is handed this object to build it from — and one of
        those plugins contributes a job that reaches two services this object is
        still in the middle of building. Resolved once and kept: a job's
        callable closes over services, so re-resolving would quietly hand out
        two closures over the same thing.
        """
        if self._jobs is None:
            self._jobs = self.plugins.scheduled_jobs(self)
        return self._jobs

    def health_checks(self) -> tuple[HealthCheck, ...]:
        """Every reason the installed plugins have to call this node unhealthy."""
        return self.plugins.health_checks(self)

    def start(self) -> None:
        """Let every plugin do what it owes the process that is starting."""
        self.plugins.startup(
            RuntimeContext(process=self.process, settings=self.settings), self
        )

    def stop(self) -> None:
        """The mirror of :meth:`start`, before adapters are released."""
        self.plugins.shutdown(
            RuntimeContext(process=self.process, settings=self.settings), self
        )

    def queue_ready(self) -> bool:
        """Whether the durable work queue can be read right now.

        A separate question from "is the database up" even though both answers
        come from the same file: a schema that has not been migrated has a
        database and no ``jobs`` table, and a controller in that state accepts
        deployments it can never run. The two health checks fail differently
        and are fixed differently, which is why there are two of them.
        """
        return self._queue_ready()

    def close(self) -> None:
        """Release adapters created by this composition root.

        Injected repositories remain the caller's responsibility. This keeps
        tests and embedded callers free to share one repository between more
        than one control plane without one instance closing another's store.
        """
        repository, self._owned_repository = self._owned_repository, None
        if repository is not None:
            repository.close()


def build_control_plane(
    settings: Settings,
    *,
    pool_connections: bool = False,
    process: ProcessKind = ProcessKind.CLI,
    plugins: PluginRegistry | None = None,
) -> ControlPlane:
    """Build a control plane wired to the real adapters and the real plugins."""
    return ControlPlane(
        settings=settings,
        pool_connections=pool_connections,
        process=process,
        plugins=plugins,
    )


class _QueuePublisher:
    """The deployment capability's ``QueueBackgroundRunner``, over the job queue.

    A two-line adapter and a deliberate one. ``deployments`` declared a port
    with a single ``enqueue`` on it and knows nothing about jobs, leases or
    fences; binding that port to this installation's queue is a composition
    decision, and putting it here is what keeps the deployment service unable
    to reach the rest of the queue's surface.
    """

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def enqueue(self, deployment_id: str) -> None:
        self._queue.enqueue_deployment(deployment_id)


def _queue_reachable(queue: JobQueue) -> bool:
    """Whether the job table answers, without changing anything in it.

    A bounded read rather than a write: a health check that enqueued would put
    a row in the table every time a load balancer asked whether this node was
    alive.

    The two failures worth reporting as "not ready" are named rather than
    caught wholesale. A missing table or a locked file is a node that cannot
    run background work and should be taken out of rotation; a `TypeError` in
    this function is a bug in the control plane, and a health check that
    swallowed it would report a healthy node forever.
    """
    try:
        queue.list_jobs(limit=1)
    except (SQLAlchemyError, OSError):
        return False
    return True
