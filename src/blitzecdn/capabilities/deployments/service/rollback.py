"""What rolling back means, apart from the run that performs it.

A rollback is an ordinary convergence of an older *release* plus three
decisions a forward deploy never has to make: which release, whether canonical
state may still be overwritten by the time the fleet has converged to it, and
what adopting it does to the zones, their rules and their records. Those
decisions have their own reason to change — they are about the meaning of "roll
back", not about running Ansible — and each of them protects an invariant that
is easy to lose in a service that is mostly about the run.

The third is the one worth naming out loud. A rollback that only converged the
old artifact would leave the edges serving one state and the control plane
asserting another, and the next ordinary reconciliation would compile the state
the control plane still holds and undo the rollback — quietly, and within the
hour. Adopting the release's *inputs* is what makes a rollback a change of
desired target rather than a temporary override, and it is why this restores
zones and rules rather than replaying a rendered document.

Written as functions over the ports the service already holds, so the service
still owns the lock, the transaction and the ordering; this owns the policy.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.domain import Deployment, DeploymentStatus
from blitzecdn.capabilities.deployments.ports import (
    DeploymentStore,
    Releases,
    RuleRestore,
    ZoneStore,
)
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.core.exceptions import ConflictError

__all__ = ["adopt_inputs", "require_unchanged_canonical", "select_target"]


def select_target(
    deployments: DeploymentStore, releases: Releases, deployment_id: str | None
) -> Deployment:
    """The deployment whose release this rollback will converge.

    Named explicitly, or else the most recent successful applied run of a
    release that is not the current one. Either way it has to be a run that
    actually reached the fleet: a check-mode run proved the play parses and a
    failed or abandoned one converged some unknowable fraction of the edges, so
    neither describes a state the fleet was ever wholly in, and neither is a
    thing to return to.

    "Not the current one" is now a comparison of release identifiers rather
    than of two serialised documents. That is the same question asked more
    cheaply, and it is exact: a release is the digest of its own contents, so
    two runs of unchanged state name one identifier by construction.

    ``compile`` rather than ``prepare``: this only needs to know what current
    state *would* compile to, and choosing a rollback target should not put a
    row in the release history for a state nobody has asked to deploy.
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

    :func:`adopt_inputs` deletes every zone and record and writes the release's
    back, so anything created since the rollback was queued is gone — and
    record writes deliberately do not take the deployment lock, which means an
    ordinary ``blitzecdn record create`` during a minutes-long fleet rollback is
    enough. Nothing conflicted, nothing failed, and the audit trail showed the
    record being created and never being removed.

    Read inside the adoption transaction, which is ``BEGIN IMMEDIATE``, so no
    writer can slip between this comparison and the restore. Raising here
    aborts that transaction and the run finalises as FAILED: the edges are
    converged to the old release but canonical state is untouched, so an
    operator can retry the rollback deliberately once they have seen what
    changed.
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


def adopt_inputs(zones: ZoneStore, rules: RuleRestore, inputs: ReleaseInputs) -> None:
    """Make the converged release's state canonical desired state.

    Two writes and an order between them. ``replace_all_records`` deletes the
    zone rows and writes them again, and a rule is keyed to its zone with ON
    DELETE CASCADE — so the rules go back after the zones, never before, or
    they would be written into a table that is about to be emptied.

    Two, and not the four a stored site would need: nothing references a site,
    because nothing stores one.

    Called only inside the caller's transaction, and only after
    :func:`require_unchanged_canonical` has agreed there is nothing to lose.
    """
    zones.replace_all_records(list(inputs.domains), list(inputs.records))
    rules.replace_all_rules(list(inputs.rules))
