"""Reading a recorded deployment as an answer to a question.

A deployment row is written by the convergence path and then read by several
callers who each want something different from it: an operator asking what
drifted, the certificate service asking whether a vhost is actually on the
edges. Those readings are rules — which runs count, and what a run's result is
allowed to be taken as evidence of — and they have their own reason to change:
none of them takes the deployment lock, moves a record through the transition
table, or runs Ansible.

Written as functions over ``DeploymentStore`` for the same reason
``service.rollback`` is: the service still owns the lock and the ordering, and
this owns only what a stored run means.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.domain import DeploymentStatus, DriftReport
from blitzecdn.capabilities.deployments.domain.snapshots import decode_snapshot
from blitzecdn.capabilities.deployments.ports import DeploymentStore
from blitzecdn.core.exceptions import ConflictError

#: How far back :func:`site_is_deployed` will look for a run to read. A bound
#: rather than the whole table because the answer comes from the *newest*
#: qualifying run — anything older cannot change it, and history is pruned to a
#: retention that may be far larger than this.
_DEPLOYMENT_LOOKBACK = 50

__all__ = ["drift_report", "site_is_deployed"]


def drift_report(deployments: DeploymentStore, deployment_id: str) -> DriftReport:
    """Read a recorded check-mode run as a drift report.

    Derived from the stored deployment rather than only from a live run, so the
    CLI and the API share one interpretation and an operator can revisit the
    answer a scheduled check produced without re-running it. The stored result
    is the structured one, so this reads the same object a live run produced
    rather than re-deriving anything.
    """
    deployment = deployments.get_deployment(deployment_id)
    if not deployment.check_mode:
        raise ConflictError(
            f"deployment {deployment.id} applied changes rather than "
            "previewing them, so its result describes what it did, not "
            "what had drifted. Run 'blitzecdn drift' instead."
        )
    return DriftReport.of(deployment)


def site_is_deployed(deployments: DeploymentStore, site_name: str) -> bool:
    """Whether the most recent real deployment carried this site.

    Check-mode runs are skipped: they proved the play parses, not that any edge
    is serving the vhost. A run narrowed by ``host_limit`` counts, because it
    did install the site somewhere — which makes this check necessary rather
    than sufficient, the same caveat rollback carries about canaries. Only the
    newest successful run is consulted; an older one listing the site says
    nothing about whether it is still deployed.
    """
    for deployment in deployments.list_deployments(limit=_DEPLOYMENT_LOOKBACK):
        if deployment.status is not DeploymentStatus.SUCCEEDED:
            continue
        if deployment.check_mode:
            continue
        snapshot = deployments.deployment_snapshot(deployment.id)
        return any(site.name == site_name for site in decode_snapshot(snapshot))
    return False
