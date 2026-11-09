"""How the deployments capability is built, and what it runs.

Assembling ``DeploymentService`` from its parts is a deployments decision, so
it lives here rather than in the composition root. Startup recovery runs from
the API process — once per node, under the deployment lock, never per CLI
command — along with the drift check.
"""

from __future__ import annotations

from pathlib import Path

from dishka import Provider, Scope, alias, provide

from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentOutcomeStore,
    DeploymentRequirementStore,
    DeploymentTargetStore,
)
from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentStore as DeploymentTable,
)
from blitzecdn.capabilities.deployments.ports import (
    ConvergenceOutcomes,
    DeploymentGateway,
    DeploymentLocker,
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    DeploymentTargets,
    EventRecorder,
    QueueBackgroundRunner,
    Releases,
    RuleRestore,
    ServingVerifier,
    SettingRestore,
    UnitOfWork,
    Workflows,
    ZoneEditor,
)
from blitzecdn.capabilities.deployments.service.convergence import (
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentPolicy,
    DeploymentService,
)
from blitzecdn.capabilities.deployments.service.rollout import Rollout
from blitzecdn.capabilities.deployments.settings import (
    CONFIGURATION,
    DeploymentsConfig,
)
from blitzecdn.capabilities.dns import RuleStore, ZoneStore
from blitzecdn.capabilities.edges import EdgeStore
from blitzecdn.capabilities.releases import Release, ReleaseReferences
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.domain.validation import matches_edge_limit
from blitzecdn.core.features import FeatureOperations, ScheduledJob
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.runtime.filesystem import (
    atomic_write_bytes,
    atomic_write_yaml,
    read_log_tail,
)

__all__ = [
    "CONFIGURATION_BUNDLE",
    "DeploymentsProvider",
    "RegisteredEdgeAddresses",
    "build_deployment_service",
    "deployments_operations",
    "publish_release",
]

#: Beside the execution document, in the controller's state directory.
CONFIGURATION_BUNDLE = "configuration-bundle.json"


def publish_release(
    release: Release, bundle: ConfigurationBundle, variables: Path
) -> None:
    """Put a release where the edge play reads it: its tree, then its document.

    The bundle first, so a document never names a bundle that is not there.
    Its path rides in the execution envelope beside the release digest,
    outside the artifact hash: where the controller keeps a file is not
    desired state.
    """
    bundle_path = variables.with_name(CONFIGURATION_BUNDLE)
    atomic_write_bytes(bundle_path, bundle.encode().encode("utf-8"))
    atomic_write_yaml(
        variables,
        {
            **release.execution_document(),
            "blitzecdn_config_release_bundle": str(bundle_path),
        },
    )


class RegisteredEdgeAddresses:
    """The fleet, seen as "which edges, and where does each answer".

    Bound to the edge store here because choosing which store answers is the
    composition root's business, and kept to two methods because that is all
    the rollout asked for — a deployment service that could reach an edge's SSH
    user would be one whose behaviour could come to depend on it.

    ``public_addresses`` is a list because an edge may be multi-homed; the
    first is used, because verification asks whether the edge answers rather
    than whether every one of its addresses does. An edge with none declared
    returns ``None``, and the rollout says what it does about that.
    """

    def __init__(self, edges: EdgeStore) -> None:
        self._edges = edges

    def selected(self, host_limit: str | None) -> list[str]:
        return [
            edge.name
            for edge in self._edges.list_edges()
            if host_limit is None or matches_edge_limit(edge.name, host_limit)
        ]

    def public_address(self, edge: str) -> str | None:
        for candidate in self._edges.list_edges():
            if candidate.name != edge:
                continue
            if candidate.public_addresses:
                return candidate.public_addresses[0]
            # Falling back to the SSH host is deliberate and is the
            # single-homed case: `Edge.public_addresses` documents an empty
            # list as "the same address the controller connects to", so
            # verification uses the address the fleet already says is public.
            return candidate.host
        return None


def build_deployment_service(
    *,
    settings: Settings,
    config: DeploymentsConfig,
    deployments: DeploymentStore,
    zones: ZoneStore,
    rules: RuleRestore,
    fleet_settings: SettingRestore,
    requirements: DeploymentRequirements,
    outcomes: ConvergenceOutcomes,
    releases: Releases,
    targets: DeploymentTargets,
    edges: EdgeStore,
    verifier: ServingVerifier,
    runner: DeploymentRunner,
    background: QueueBackgroundRunner,
    dns: ZoneEditor,
    workflows: Workflows,
    events: EventRecorder,
    uow: UnitOfWork,
    clock: Clock,
) -> DeploymentService:
    """Assemble the capability from its policy, its state, and its runners.

    Every collaborator arrives as an argument: choosing concrete adapters and
    services is the composition root's decision. How they form the policy,
    persistence and execution parts is decided here.

    Nothing here renders a desired-state document. Deriving the hosts, merging
    every feature's contribution and validating the result belong to the
    releases capability; this one holds the ``Releases`` port and an atomic
    writer, which is the whole of its contact with configuration.
    """
    addresses = RegisteredEdgeAddresses(edges)
    return DeploymentService(
        policy=DeploymentPolicy(
            run_dir=settings.run_dir,
            generated_vars_path=settings.generated_vars_path,
            output_limit_bytes=settings.output_limit_bytes,
            history_retention=settings.history_retention,
            deployment_retention=config.retention,
            runtime_errors=settings.validate_runtime,
        ),
        persistence=DeploymentPersistence(
            deployments=deployments,
            targets=targets,
            zones=zones,
            rules=rules,
            settings=fleet_settings,
            uow=uow,
            requirements=requirements,
            outcomes=outcomes,
        ),
        execution=DeploymentExecution(
            runner=runner,
            background=background,
            read_log=read_log_tail,
            write_yaml=atomic_write_yaml,
            rollout=Rollout(
                targets=targets,
                runner=runner,
                addresses=addresses,
                verifier=verifier,
                clock=clock,
                # The PREPARE phase writes the release's configuration and its
                # execution document beside it, for the edge play to stage.
                publish=lambda release: publish_release(
                    release,
                    releases.configuration(release.id),
                    settings.generated_vars_path,
                ),
            ),
            addresses=addresses,
        ),
        events=events,
        dns=dns,
        releases=releases,
        workflows=workflows,
        clock=clock,
    )


def deployments_operations(
    *, deployments: DeploymentService, drift_check_interval_seconds: int
) -> FeatureOperations:
    """The drift check, and node-startup recovery of interrupted deployments."""

    def check(operator: str) -> None:
        deployments.check_drift(operator)

    def startup() -> None:
        deployments.initialize()

    return FeatureOperations(
        feature="deployments",
        scheduled_jobs=(
            ScheduledJob(
                feature="deployments",
                name="check-drift",
                interval_seconds=drift_check_interval_seconds,
                run=check,
                jitter_seconds=min(600, drift_check_interval_seconds // 10),
            ),
        ),
        startup=startup,
    )


class DeploymentsProvider(Provider):
    """The deployment tables, the configuration, and the convergence service."""

    scope = Scope.APP

    @provide
    def config(
        self, capability_config: ResolvedCapabilityEnvironment
    ) -> DeploymentsConfig:
        return capability_config.for_feature(CONFIGURATION.feature).as_model(
            DeploymentsConfig
        )

    @provide
    def deployments(self, database: Database) -> DeploymentStore:
        return DeploymentTable(database)

    @provide
    def requirements(self, database: Database) -> DeploymentRequirements:
        return DeploymentRequirementStore(database)

    @provide
    def outcomes(self, database: Database) -> ConvergenceOutcomes:
        return DeploymentOutcomeStore(database)

    @provide
    def targets(self, database: Database) -> DeploymentTargets:
        return DeploymentTargetStore(database)

    # Release pruning asks which releases are still rollback targets.
    referenced = alias(source=DeploymentStore, provides=ReleaseReferences)
    rules = alias(source=RuleStore, provides=RuleRestore)
    locker = alias(source=DeploymentRunner, provides=DeploymentLocker)
    gateway = alias(source=DeploymentService, provides=DeploymentGateway)

    @provide
    def service(
        self,
        settings: Settings,
        config: DeploymentsConfig,
        deployments: DeploymentStore,
        zones: ZoneStore,
        rules: RuleRestore,
        fleet_settings: SettingRestore,
        requirements: DeploymentRequirements,
        outcomes: ConvergenceOutcomes,
        releases: Releases,
        targets: DeploymentTargets,
        edges: EdgeStore,
        verifier: ServingVerifier,
        runner: DeploymentRunner,
        background: QueueBackgroundRunner,
        dns: ZoneEditor,
        workflows: Workflows,
        events: EventRecorder,
        uow: UnitOfWork,
        clock: Clock,
    ) -> DeploymentService:
        return build_deployment_service(
            settings=settings,
            config=config,
            deployments=deployments,
            zones=zones,
            rules=rules,
            fleet_settings=fleet_settings,
            requirements=requirements,
            outcomes=outcomes,
            releases=releases,
            targets=targets,
            edges=edges,
            verifier=verifier,
            runner=runner,
            background=background,
            dns=dns,
            workflows=workflows,
            events=events,
            uow=uow,
            clock=clock,
        )
