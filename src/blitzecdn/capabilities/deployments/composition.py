"""How the deployments capability is built.

The same shape as :mod:`blitzecdn.capabilities.dns.composition` and the only
built-in with real assembly to do. ``DeploymentService`` is three dataclasses
and a writer before it is a service, and that three-part shape is the
capability's own: what belongs in ``DeploymentPolicy`` rather than in
``DeploymentExecution`` is a question about deployments, answered by the people
changing deployments, and answering it in the composition root would put it in
the one file that is supposed to know *which* concrete things are wired and not
how any capability is put together internally — where adding a collaborator
here means editing that file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.deployments.ports import (
    DeploymentRequirements,
    DeploymentRunner,
    DeploymentStore,
    DeploymentTargets,
    QueueBackgroundRunner,
    Releases,
    RuleRestore,
    ServingVerifier,
)
from blitzecdn.capabilities.deployments.service.convergence import (
    DeploymentExecution,
    DeploymentPersistence,
    DeploymentPolicy,
    DeploymentService,
)
from blitzecdn.capabilities.deployments.service.rollout import Rollout
from blitzecdn.capabilities.dns.ports import ZoneStore
from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.core.domain.validation import matches_edge_limit
from blitzecdn.core.runtime.filesystem import atomic_write_yaml, read_log_tail

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

__all__ = ["RegisteredEdgeAddresses", "build_deployment_service"]


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
    platform: ControlPlane,
    *,
    deployments: DeploymentStore,
    zones: ZoneStore,
    rules: RuleRestore,
    requirements: DeploymentRequirements,
    releases: Releases,
    targets: DeploymentTargets,
    edges: EdgeStore,
    verifier: ServingVerifier,
    runner: DeploymentRunner,
    background: QueueBackgroundRunner,
) -> DeploymentService:
    """Assemble the capability from its policy, its state, and its runners.

    ``runner``, ``background`` and ``releases`` arrive as arguments because
    choosing the Ansible adapter, the queue adapter and the release service is
    the composition root's decision and stays there. Everything else is put
    together here.

    Nothing here renders a desired-state document any more. Deriving the hosts,
    merging every plugin's contribution and validating the result belong to the
    releases capability; this one holds the ``Releases`` port and an atomic
    writer, which is the whole of its contact with configuration.
    """
    addresses = RegisteredEdgeAddresses(edges)
    service = DeploymentService(
        policy=DeploymentPolicy(
            run_dir=platform.settings.run_dir,
            generated_vars_path=platform.settings.generated_vars_path,
            output_limit_bytes=platform.settings.output_limit_bytes,
            history_retention=platform.settings.history_retention,
            runtime_errors=platform.settings.validate_runtime,
        ),
        persistence=DeploymentPersistence(
            deployments=deployments,
            targets=targets,
            zones=zones,
            rules=rules,
            uow=platform.transactions,
            requirements=requirements,
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
                # Bound after the service exists, below: the rollout publishes
                # the artifact through the service that owns the path it goes
                # to, and neither can be built before the other without one of
                # them learning the other's job.
                publish=lambda _release: None,
            ),
            addresses=addresses,
        ),
        events=platform.events,
        dns=platform.dns,
        releases=releases,
        workflows=platform.workflows,
    )
    # The one late binding in this file, and it is a knot rather than an
    # afterthought: the rollout's PREPARE phase writes the release's artifact
    # to `generated_vars_path`, and the object that knows that path is the
    # service the rollout is a collaborator of. Passing the path down instead
    # would give the rollout a second opinion about where desired state lives.
    service.execution.rollout.publish = lambda release: service.publish_artifact(
        release, platform.settings.generated_vars_path
    )
    return service
