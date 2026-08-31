"""What the scheduler asks for, and the convergence it may still owe."""

from __future__ import annotations

from blitzecdn.core.operations import MaintenanceOperation
from blitzecdn.features.deployments.domain import DeploymentRequirementKind
from blitzecdn.features.maintenance.ports import (
    AutomaticSsl,
    Certificates,
    Deployments,
    Requirements,
)


class MaintenanceService:
    def __init__(
        self,
        *,
        certificates: Certificates,
        automatic_ssl: AutomaticSsl,
        deployments: Deployments,
        requirements: Requirements,
        renewal_budget_seconds: int,
    ) -> None:
        self._certificates = certificates
        self._automatic_ssl = automatic_ssl
        self._deployments = deployments
        self._requirements = requirements
        self._renewal_budget_seconds = renewal_budget_seconds

    def run(self, operation: MaintenanceOperation, operator: str = "scheduler") -> None:
        if operation is MaintenanceOperation.RECONCILE_CERTIFICATES:
            result = self._certificates.reconcile_certificates(operator)
            if result.issued:
                self._automatic_ssl.reconcile(operator)
        elif operation is MaintenanceOperation.RECONCILE_AUTOMATIC_SSL:
            self._automatic_ssl.reconcile(operator)
        elif operation is MaintenanceOperation.RENEW_CERTIFICATES:
            self._certificates.renew_certificates(
                operator, budget_seconds=self._renewal_budget_seconds
            )
        elif operation is MaintenanceOperation.CHECK_DRIFT:
            self._deployments.check_drift(operator)

        if self._requirements.pending(DeploymentRequirementKind.CERTIFICATES):
            self._deployments.submit_deployment(operator)
