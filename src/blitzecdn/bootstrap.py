"""The composition root.

This is the one module that knows both halves: it builds concrete adapters from
:mod:`blitzecdn.core` and the capability packages, then injects them into capability
services. Production wiring lives here and nowhere else, so
"what does a real control plane consist of" is answerable by reading one
constructor.

Then it loads the plugins. The order matters and is the whole architecture in
four lines: adapters, services, plugins, contributions. Discovery is the one
step that runs first, because an installed package's Ansible roles have to be
in the search path before the runner that will resolve them exists — but a
plugin is still *given* the control plane last, when every service is built.
Services are built with explicit constructor injection and know nothing about
plugins; plugins are given the finished control plane and use it to register
what they contribute — routes, commands, jobs, health checks, desired state.
Nothing flows the other way, and no service is ever *looked up*:
`platform.cache` in a `plugin.py` is a typed attribute read once at
registration, not a resolution step in a request.

``ControlPlane`` is that constructor and nothing else. It holds the capability
services and the ports the entry layers read through, and it forwards no calls:
the CLI and the API reach the service that owns the work —
``control_plane.dns.create_record(...)`` — rather than a method here that would
restate a signature already written on the service.

Everything reachable from here is a service or a port. The concrete
``Repository`` is deliberately not an attribute: an entry layer that could
reach it would be one import away from calling SQLite directly, which is easy
to do by accident in a read path and invisible in review — so the rule is
written down rather than assumed.

The queue is reached through :mod:`blitzecdn.core.broker` and never
through :mod:`blitzecdn.worker`. The worker is an entry point that builds a
control plane, so importing it from here would point the arrow both ways;
``tests/architecture/test_layering.py`` refuses that import by name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from blitzecdn.capabilities.deployments.desired_state import DesiredStateRenderer
from blitzecdn.capabilities.deployments.ports import (
    DeploymentLocker,
    DeploymentRequirements,
    DeploymentRunner,
    QueueBackgroundRunner,
)
from blitzecdn.capabilities.deployments.service import (
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentPolicy,
    DeploymentService,
)
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.ports import EdgeRunner
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.edges.probe import OriginProbe
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.capabilities.sites.ports import SiteReader
from blitzecdn.capabilities.sites.service import SiteService
from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.broker import DramatiqBackgroundRunner, redis_ready
from blitzecdn.core.config import Settings
from blitzecdn.core.database import Repository
from blitzecdn.core.filesystem import atomic_write_yaml, read_log_tail
from blitzecdn.core.operation_ports import AuditTrail, PlaybookRunner
from blitzecdn.core.plugins import (
    HealthCheck,
    PluginRegistry,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
    ValidationResult,
    load_plugins,
    resolve_capability_environment,
    resolve_edge_capability_roles,
    resolve_edge_modules,
    resolve_host_capability_roles,
    resolve_nginx_resources,
    resolve_role_search_path,
    resolve_teardown_capability_roles,
)
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.workflows import WorkflowCoordinator


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
    ``CacheRunner`` appears in this list any more. That is the difference
    between a built-in, whose port core may name, and a distribution core has
    never heard of.
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
        edges_store: EdgeStorePort | None = None,
        background: QueueBackgroundRunner | None = None,
        broker_ready: Callable[[str], bool] | None = None,
        pool_connections: bool = False,
        plugins: PluginRegistry | None = None,
        process: ProcessKind = ProcessKind.CLI,
    ) -> None:
        self.settings = settings
        #: Which process this control plane is being built for. Lifecycle
        #: contributions branch on it; nothing else does.
        self.process = process
        store = repository or Repository(
            settings.database_path, pool_connections=pool_connections
        )
        self._owned_repository = store if repository is None else None
        # Discovered before the adapters, because one adapter is built from
        # what is installed: Ansible resolves a role name against a single
        # process-wide search path, so a package that ships a role has to be
        # known before the runner exists. Nothing else reads the registry this
        # early — plugins are still *given* the control plane last, once every
        # service they might register against has been built.
        self.plugins = plugins if plugins is not None else load_plugins()
        # Before anything is wired: an optional capability this installation
        # says it depends on has to actually be installed. Detaching a package
        # is a supported operation, so its absence is not an error on its own —
        # but a configuration that still asks for it must fail here, with the
        # token named, rather than start and behave as if the capability had
        # been switched off. The tokens are configuration and the answer is
        # plugin metadata; nothing in between names a capability.
        self.plugins.require(
            self.settings.required_capabilities,
            subject="this installation's `required_capabilities`",
        )
        self._wire_adapters(
            store=store,
            runner=runner,
            origin_probe=origin_probe,
            edges_store=edges_store,
            background=background,
            broker_ready=broker_ready,
        )
        self._jobs: dict[str, ScheduledJob] | None = None
        self._wire_services(store)

    def _wire_adapters(
        self,
        *,
        store: Repository,
        runner: FleetRunner | None,
        origin_probe: OriginProbePort | None,
        edges_store: EdgeStorePort | None,
        background: QueueBackgroundRunner | None,
        broker_ready: Callable[[str], bool] | None,
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
        # Each installed package's own configuration, resolved once here and
        # never read out of `Settings` by the packages themselves. Flat for
        # Ansible, scoped for the controller: `platform.capability_config` is
        # how a capability reads what it claimed — a credential or a setting —
        # and it can reach nothing it did not claim.
        #
        # It answers for a different contribution than the roles above.
        # Configuration used to ride on `AnsibleContribution`, which was true
        # of a secret forwarded into a play and false of every setting that
        # never leaves the controller.
        self.capability_config = resolve_capability_environment(
            self.plugins.configuration_contributions(),
            self.settings.capability_environment,
            self.settings.capability_config_file,
            self.settings.state_dir,
        )
        self._runner = runner or AnsibleRunner(
            self.settings,
            self._edges_store,
            # An installed capability's roles, alongside core's. This is the
            # one place that knows both halves: the registry answers what is
            # installed, `resolve_role_search_path` decides the order and
            # refuses a role two packages both ship, and the runner is handed
            # a finished list. Detaching a package removes its directory from
            # this list with nothing in core edited.
            resolve_role_search_path(
                self.settings.ansible_dir / "roles",
                contributions,
            ),
            # And which of those roles core's own plays run, in each of the
            # three slots: two in the edge play, one in the decommission play.
            # Four questions, one source — a package that ships a role only its
            # own plays reach declares the directory and no slot at all.
            capability_roles=resolve_edge_capability_roles(contributions),
            host_capability_roles=resolve_host_capability_roles(contributions),
            teardown_capability_roles=resolve_teardown_capability_roles(contributions),
            nginx_resources=nginx_resources,
            # And the dynamic modules those resources need loaded. The same
            # question one level down: a contributed `brotli` directive is a
            # syntax error on an edge that never loaded the module, and an
            # edge that loads one no installed capability asked for is the
            # image enumerating capabilities instead of the controller.
            edge_modules=edge_modules,
            capability_environment=self.capability_config.environment,
        )
        self._origin_probe = origin_probe or OriginProbe(self.settings)
        self.origin_probe: OriginProbePort = self._origin_probe
        self.deployment_lock: DeploymentLocker = self._runner
        self._background = background or DramatiqBackgroundRunner(
            str(self.settings.redis_url)
        )
        readiness_probe = broker_ready or redis_ready
        self._broker_ready: Callable[[], bool] = lambda: readiness_probe(
            str(self.settings.redis_url)
        )

    def _wire_services(self, store: Repository) -> None:
        """Build cross-cutting services, then capability-oriented services."""

        # Entry layers receive only the read side of the audit trail, so they
        # cannot manufacture an event for an action no service performed.
        self.audit: AuditTrail = store.audit_log

        # The two contracts an installed capability builds itself from, both
        # typed as ports rather than as the concrete things behind them.
        #
        # `sites` is the read side of the site model. It is a port for the same
        # reason `audit` is: a reader is not a repository, and a package handed
        # one can answer "which hostnames does the fleet serve" without being
        # able to write a site or reach SQLite. A package that genuinely has to
        # write one — `blitzecdn-certificates` activating a certificate it just
        # issued — reaches `site_editor` and narrows it to the two methods it
        # calls with a port of its own, exactly as it used to do with the zone
        # editor. `fleet` runs a named play
        # across the edges in scope and knows nothing about what any play is
        # for. Between them they are the whole of what an optional package
        # needs and deliberately less than what a built-in service receives.
        self.sites: SiteReader = store.sites
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
        # Both halves of the site model, and the split between them is the
        # architecture: `site_editor` owns everything about how a site is
        # served, `dns` owns which hostnames route to it. The same store is
        # behind both, seen through two ports that cannot write each other's
        # half — which is what makes "who wrote this field" answerable.
        self.site_editor = SiteService(
            sites=store.sites,
            events=self.events,
            uow=store,
        )
        self.dns = DnsService(
            zones=store.zones,
            sites=store.sites,
            events=self.events,
            uow=store,
        )
        self._wire_capability_services(store)

    def _wire_capability_services(self, store: Repository) -> None:
        """Build required deployment, edge, and maintenance capabilities."""
        # Every variable in a desired-state document comes from a plugin. This
        # is the one place that knows the plugin registry can answer for all of
        # them, which is what keeps `capabilities/deployments` free of it.
        renderer = DesiredStateRenderer(
            allow_empty_sites=self.settings.allow_empty_sites,
            contributors=self.plugins.contributions_for(self),
            write_yaml=atomic_write_yaml,
        )
        self.deployments = DeploymentService(
            policy=DeploymentPolicy(
                run_dir=self.settings.run_dir,
                generated_vars_path=self.settings.generated_vars_path,
                output_limit_bytes=self.settings.output_limit_bytes,
                history_retention=self.settings.history_retention,
                runtime_errors=self.settings.validate_runtime,
            ),
            persistence=DeploymentPersistence(
                deployments=store.deployments,
                zones=store.zones,
                sites=store.sites,
                uow=store,
                requirements=store.deployment_requirements,
            ),
            execution=DeploymentExecution(
                runner=self._runner,
                background=self._background,
                read_log=read_log_tail,
                renderer=renderer,
                validator=_SiteValidator(self.plugins, self),
            ),
            events=self.events,
            dns=self.dns,
            workflows=self.workflows,
        )
        self.edges = EdgeOperationsService(
            events=self.events,
            runner=self._runner,
            edges=self._edges_store,
            uow=store,
        )
        self.maintenance = MaintenanceService(
            jobs=lambda: self.jobs,
            deployments=self.deployments,
            requirements=store.deployment_requirements,
        )

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

    def broker_ready(self) -> bool:
        """Whether the durable work broker is currently reachable."""
        return self._broker_ready()

    def close(self) -> None:
        """Release adapters created by this composition root.

        Injected repositories remain the caller's responsibility. This keeps
        tests and embedded callers free to share one repository between more
        than one control plane without one instance closing another's store.
        """
        repository, self._owned_repository = self._owned_repository, None
        if repository is not None:
            repository.close()


class _SiteValidator:
    """The deployment service's view of "what do the plugins object to".

    A two-line class rather than a lambda so the deployment service is handed
    something that reads like the port it declared, and so the binding of a
    registry to a platform stays here in the composition root.
    """

    def __init__(self, plugins: PluginRegistry, platform: ControlPlane) -> None:
        self._plugins = plugins
        self._platform = platform

    def validate_site(self, site: CdnSite) -> ValidationResult:
        return self._plugins.validate_site(site, self._platform)


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
