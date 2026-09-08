"""What rolling back means, apart from the run that performs it.

A rollback is an ordinary convergence of an older snapshot plus three decisions
that a forward deploy never has to make: which snapshot, whether canonical
state may still be overwritten by the time the fleet has converged to it, and
what adopting it does to the zones, their rules and their records. Those
decisions have their own reason to change — they are about the meaning of
"roll back", not about running Ansible — and each of them protects an invariant
that is easy to lose in a service that is mostly about the run.

Written as functions over the ports the service already holds, so the service
still owns the lock, the transaction and the ordering; this owns the policy.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.domain import Deployment, DeploymentStatus
from blitzecdn.capabilities.deployments.domain.snapshots import (
    decode_snapshot_state,
    snapshot_digest,
)
from blitzecdn.capabilities.deployments.ports import (
    DeploymentStore,
    RuleRestore,
    ZoneStore,
)
from blitzecdn.core.exceptions import ConflictError

__all__ = ["adopt_snapshot", "require_unchanged_canonical", "select_target"]


def select_target(
    deployments: DeploymentStore, deployment_id: str | None
) -> Deployment:
    """The deployment whose snapshot this rollback will converge.

    Named explicitly, or else the most recent successful applied run that is
    not the current state. Either way it has to be a run that actually reached
    the fleet: a check-mode run proved the play parses and a failed or
    abandoned one converged some unknowable fraction of the edges, so neither
    describes a state the fleet was ever wholly in, and neither is a thing to
    return to.
    """
    target = (
        deployments.get_deployment(deployment_id)
        if deployment_id
        else deployments.successful_rollback_target(deployments.snapshot())
    )
    if target.check_mode or target.status is not DeploymentStatus.SUCCEEDED:
        raise ConflictError("rollback target must be a successful applied deployment")
    return target


def require_unchanged_canonical(
    deployments: DeploymentStore, deployment: Deployment
) -> None:
    """Refuse to restore wholesale over a change made while we converged.

    :func:`adopt_snapshot` deletes every zone and record and writes the
    snapshot's back, so anything created since the rollback was queued is
    gone — and record writes deliberately do not take the deployment lock,
    which means an ordinary ``blitzecdn record create`` during a
    minutes-long fleet rollback is enough. Nothing conflicted, nothing
    failed, and the audit trail showed the record being created and never
    being removed.

    Read inside the adoption transaction, which is ``BEGIN IMMEDIATE``, so
    no writer can slip between this comparison and the restore. Raising
    here aborts that transaction and the run finalises as FAILED: the edges
    are converged to the old snapshot but canonical state is untouched, so
    an operator can retry the rollback deliberately once they have seen
    what changed.
    """
    if deployment.canonical_digest is None:
        return
    current = snapshot_digest(deployments.snapshot())
    if current != deployment.canonical_digest:
        raise ConflictError(
            "desired state changed while this rollback was converging, so "
            "adopting the older snapshot would delete whatever was written. "
            "The edges were converged to it; canonical records were left "
            "alone. Review the change and roll back again if it should go."
        )


def adopt_snapshot(zones: ZoneStore, rules: RuleRestore, snapshot: str) -> None:
    """Make the converged snapshot canonical desired state.

    Two writes and an order between them. ``replace_all_records`` deletes the
    zone rows and writes them again, and a rule is keyed to its zone with ON
    DELETE CASCADE — so the rules go back after the zones, never before, or
    they would be written into a table that is about to be emptied.

    It used to be four calls with a comment about foreign keys between them: a
    record referenced the site that served it, so the records had to come out
    before the sites could be replaced and go back in afterwards. Nothing
    references a site now, because nothing stores one.

    Called only inside the caller's transaction, and only after
    :func:`require_unchanged_canonical` has agreed there is nothing to lose.
    """
    domains, records, restored_rules = decode_snapshot_state(snapshot)
    zones.replace_all_records(domains, records)
    rules.replace_all_rules(restored_rules)
