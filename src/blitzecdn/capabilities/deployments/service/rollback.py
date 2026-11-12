"""What rolling back means, apart from the run that performs it.

A rollback converges an older release and decides three things a deploy never
does: which release, whether canonical state may still be overwritten, and how
to adopt it. Adopting the release's inputs — zones, rules, records — is what
stops the next reconciliation quietly undoing the rollback. Functions over the
service's ports; the service keeps the lock and the transaction.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.domain import Deployment, DeploymentStatus
from blitzecdn.capabilities.deployments.ports import (
    DeploymentStore,
    Releases,
    RuleRestore,
    SettingRestore,
    ZoneStore,
)
from blitzecdn.capabilities.releases import ReleaseInputs
from blitzecdn.core.exceptions import ConflictError, NotFoundError

__all__ = [
    "CANONICAL_CHANGED",
    "adopt_inputs",
    "canonical_changed",
    "select_target",
]


def select_target(
    deployments: DeploymentStore, deployment_id: str | None
) -> Deployment:
    """The deployment whose release this rollback will converge.

    Named, or the newest successful full-fleet run of a release other than the
    one the fleet was last moved to: the newest real run that reached any edge,
    canary or not. That includes a failed run that committed some edges before
    stopping, so rolling back repairs a partial rollout. Desired state is not the
    reference: with undeployed edits it matches no run, and the "rollback" would
    re-converge the live release and discard the edits instead of going back
    one. Check-mode, failed and abandoned runs are never a target: they do not
    describe a state the whole fleet was in. Release ids are content digests, so
    "different" is exact.
    """
    if deployment_id:
        target = deployments.get_deployment(deployment_id)
    else:
        live = deployments.live_release()
        if live is None:
            raise NotFoundError("no deployment has reached an edge to roll back")
        target = deployments.successful_rollback_target(live)
    if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
        raise ConflictError("rollback target must be a successful applied deployment")
    return target


#: Why a rollback that converged the edges was not adopted.
CANONICAL_CHANGED = (
    "desired state changed while this rollback was converging, so adopting "
    "the older release would delete whatever was written. The edges were "
    "converged to it; canonical records were left alone, and a fleet "
    "deployment of current desired state is now owed. Review the change and "
    "roll back again if it should go."
)


def canonical_changed(releases: Releases, deployment: Deployment) -> bool:
    """Whether desired state moved while this rollback converged.

    Record writes take no deployment lock, so a ``blitzecdn record add`` during
    the rollback would otherwise be silently deleted by adoption. Read inside the
    ``BEGIN IMMEDIATE`` adoption transaction; when it moved, the caller leaves
    canonical state untouched, records the deployment FAILED with its real run
    result, and owes the fleet a convergence of current desired state.
    """
    if deployment.canonical_digest is None:
        return False
    return releases.inputs().digest != deployment.canonical_digest


def adopt_inputs(
    zones: ZoneStore,
    rules: RuleRestore,
    settings: SettingRestore,
    inputs: ReleaseInputs,
) -> None:
    """Make the converged release's state canonical desired state.

    Rules go back after the zones, which ``replace_all_records`` rewrites and
    rules cascade from. Called only inside the caller's transaction, after
    :func:`canonical_changed`.
    """
    zones.replace_all_records(list(inputs.domains), list(inputs.records))
    rules.replace_all_rules(list(inputs.rules))
    settings.replace_all_settings(inputs.settings)
