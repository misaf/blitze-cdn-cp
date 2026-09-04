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

What it decides is *which concrete thing*, never *how a capability is put
together*. Each capability is built by a ``composition.py`` beside its own
service, so that "how is this assembled" has one answer whichever side of the
packaging boundary a capability is on — ``capabilities/deployments`` and
``blitzecdn-cache`` answer it in a file of the same name doing the same job.
This file passes each one the persistence slice it declared a port for and the
adapters chosen above, and that argument list is the whole of a built-in's
privilege over a package: everything else both kinds read from ``platform``.

The difference is deliberate and is why the two are not one. A package is
handed only what core publishes — ``settings``, ``sites``, ``events``,
``fleet`` — and a built-in is additionally handed a store, because the built-in
services *are* what the platform publishes. Putting those stores on
``ControlPlane`` so that every capability could build itself from one argument
would publish the write side of the site model to every entry layer, which is
the next paragraph's rule.

Everything reachable from here is a service or a port. The concrete
``Repository`` is deliberately not an attribute: an entry layer that could
reach it would be one import away from calling SQLite directly, which is easy
to do by accident in a read path and invisible in review — so the rule is
written down rather than assumed.

The queue is reached through :mod:`blitzecdn.core.runtime.broker` and never
through :mod:`blitzecdn.worker`. The worker is an entry point that builds a
control plane, so importing it from here would point the arrow both ways;
``tests/architecture/test_layering.py`` refuses that import by name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from blitzecdn.capabilities.deployments.composition import build_deployment_service
from blitzecdn.capabilities.deployments.ports import (
    DeploymentLocker,
    DeploymentRequirements,
    DeploymentRunner,
    QueueBackgroundRunner,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.composition import build_dns_service
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.adapters.probe import OriginProbe
from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.capabilities.edges.composition import build_edge_operations_service
from blitzecdn.capabilities.edges.ports import EdgeRunner
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.edges.ports import OriginProbe as OriginProbePort
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.capabilities.maintenance.composition import build_maintenance_service
from blitzecdn.capabilities.sites.composition import build_site_service
from blitzecdn.capabilities.sites.ports import SiteReader
from blitzecdn.capabilities.sites.service import SiteService
from blitzecdn.core.ansible import AnsibleRunner
from blitzecdn.core.application.workflows import WorkflowCoordinator
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
from blitzecdn.core.runtime.broker import DramatiqBackgroundRunner, redis_ready
from blitzecdn.persistence import Repository

#: Every capability this distribution ships, in dependency order — a plugin is
#: registered after the capabilities it builds on, which is what makes the CLI's
#: command order and the API's route order stable rather than incidental.
#:
#: Order is a presentation decision here and nothing more. Desired-state
#: merging is deliberately order-independent (see `registry.merge_variables`),
#: so moving a line in this tuple can never change what an edge converges to.
#:
#: It lives here rather than in `core.plugins.discovery`, where it was, for the
#: same reason `Repository` lives beside it: choosing which parts make one
#: control plane is composition. Core held these eight strings without
#: importing them, so `test_core_imports_no_capability` stayed green while the
#: foundation carried the roster of the tree it supports, and adding a built-in
#: capability meant editing `core`. Naming is knowing.
BUILTIN_PLUGINS: tuple[str, ...] = (
    # The capability contracts first: nothing they contribute depends on
    # another capability being registered, and `sites` composes their policy.
    "blitzecdn.capabilities.http.plugin",
    "blitzecdn.capabilities.sites.plugin",
    "blitzecdn.capabilities.dns.plugin",
    "blitzecdn.capabilities.edges.plugin",
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
        self.plugins = plugins if plugins is not None else load_control_plane_plugins()
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
            # The fleet as core reads it: host names and the group they form.
            # Core declares that port and `edges` satisfies it, so running a
            # playbook does not make `core.ansible` import a capability.
            EdgeRoster(self._edges_store),
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
        self.site_editor: SiteService = build_site_service(self, sites=store.sites)
        self.dns: DnsService = build_dns_service(
            self, zones=store.zones, sites=store.sites
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
            sites=store.sites,
            requirements=store.deployment_requirements,
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
