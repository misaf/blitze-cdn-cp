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
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.core.exceptions import ConflictError

__all__ = ["adopt_inputs", "require_unchanged_canonical", "select_target"]


def select_target(
    deployments: DeploymentStore, releases: Releases, deployment_id: str | None
) -> Deployment:
    """The deployment whose release this rollback will converge.

    Named, or the newest successful applied run of a different release: check-mode,
    failed and abandoned runs never describe a state the whole fleet was in.
    Release ids are content digests, so "different" is exact. Uses ``compile`` so
    choosing writes no release history.
    """
    target = (
        deployments.get_deployment(deployment_id)
        if deployment_id
        else deployments.successful_rollback_target(releases.compile().id)
    )
    if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
        raise ConflictError("rollback target must be a successful applied deployment")
    return target


def require_unchanged_canonical(releases: Releases, deployment: Deployment) -> None:
    """Refuse to restore wholesale over a change made while we converged.

    Record writes take no deployment lock, so a ``blitzecdn record add`` during
    the rollback would otherwise be silently deleted. Read inside the
    ``BEGIN IMMEDIATE`` adoption transaction; raising leaves canonical state
    untouched and the deployment FAILED, for a deliberate retry.
    """
    if deployment.canonical_digest is None:
        return
    if releases.inputs().digest != deployment.canonical_digest:
        raise ConflictError(
            "desired state changed while this rollback was converging, so "
            "adopting the older release would delete whatever was written. "
            "The edges were converged to it; canonical records were left "
            "alone. Review the change and roll back again if it should go."
        )


def adopt_inputs(
    zones: ZoneStore,
    rules: RuleRestore,
    settings: SettingRestore,
    inputs: ReleaseInputs,
) -> None:
    """Make the converged release's state canonical desired state.

    Rules go back after the zones, which ``replace_all_records`` rewrites and
    rules cascade from. Called only inside the caller's transaction, after
    :func:`require_unchanged_canonical`.
    """
    zones.replace_all_records(list(inputs.domains), list(inputs.records))
    rules.replace_all_rules(list(inputs.rules))
    settings.replace_all_settings(inputs.settings)
