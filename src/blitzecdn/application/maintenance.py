"""Application orchestration for scheduled cross-feature maintenance."""

from __future__ import annotations

from typing import Protocol

from blitzecdn.domain.automatic_ssl import SslAutomaticReconciliation
from blitzecdn.domain.certificates import ReconciliationResult, RenewalResult
from blitzecdn.domain.deployments import Deployment, DriftReport
from blitzecdn.domain.operations import MaintenanceOperation


class _Certificates(Protocol):
    def reconcile_certificates(self, operator: str) -> ReconciliationResult: ...
    def renew_certificates(
        self, operator: str, *, budget_seconds: float | None = None
    ) -> RenewalResult: ...


class _AutomaticSsl(Protocol):
    def reconcile(self, operator: str) -> SslAutomaticReconciliation: ...


class _Deployments(Protocol):
    def check_drift(self, operator: str) -> DriftReport: ...
    def submit_deployment(self, operator: str) -> Deployment: ...


class _Requirements(Protocol):
    def pending(self, kind: str) -> bool: ...


class MaintenanceService:
    def __init__(
        self,
        *,
        certificates: _Certificates,
        automatic_ssl: _AutomaticSsl,
        deployments: _Deployments,
        requirements: _Requirements,
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

        if self._requirements.pending("certificates"):
            self._deployments.submit_deployment(operator)
