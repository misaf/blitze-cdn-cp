"""Read-side deployment use cases, separate from convergence commands."""

from blitzecdn.application.ports.deployments import DeploymentStore
from blitzecdn.domain.deployments import Deployment, DeploymentStatus, DriftReport
from blitzecdn.domain.snapshots import decode_snapshot
from blitzecdn.exceptions import ConflictError

_DEPLOYMENT_LOOKBACK = 50


class DeploymentQueries:
    def __init__(self, deployments: DeploymentStore) -> None:
        self._deployments = deployments

    def drift_report(self, deployment_id: str) -> DriftReport:
        deployment = self._deployments.get_deployment(deployment_id)
        if not deployment.check_mode:
            raise ConflictError(
                f"deployment {deployment.id} applied changes rather than "
                "previewing them, so its result describes what it did, not "
                "what had drifted. Run 'blitzecdn drift' instead."
            )
        return DriftReport.of(deployment)

    def get(self, deployment_id: str) -> Deployment:
        return self._deployments.get_deployment(deployment_id)

    def list(self, limit: int = 20) -> list[Deployment]:
        return self._deployments.list_deployments(limit)

    def site_is_deployed(self, site_name: str) -> bool:
        for deployment in self._deployments.list_deployments(
            limit=_DEPLOYMENT_LOOKBACK
        ):
            if deployment.status is not DeploymentStatus.SUCCEEDED:
                continue
            if deployment.check_mode:
                continue
            snapshot = self._deployments.deployment_snapshot(deployment.id)
            return any(site.name == site_name for site in decode_snapshot(snapshot))
        return False
