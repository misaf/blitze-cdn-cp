"""The slice of each feature a maintenance run actually uses."""

from __future__ import annotations

from typing import Protocol

from blitzecdn.features.automatic_ssl.domain import SslAutomaticReconciliation
from blitzecdn.features.certificates.domain import ReconciliationResult, RenewalResult
from blitzecdn.features.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DriftReport,
)

__all__ = ["AutomaticSsl", "Certificates", "Deployments", "Requirements"]


class Certificates(Protocol):
    def reconcile_certificates(self, operator: str) -> ReconciliationResult: ...
    def renew_certificates(
        self, operator: str, *, budget_seconds: float | None = None
    ) -> RenewalResult: ...


class AutomaticSsl(Protocol):
    def reconcile(self, operator: str) -> SslAutomaticReconciliation: ...


class Deployments(Protocol):
    def check_drift(self, operator: str) -> DriftReport: ...
    def submit_deployment(self, operator: str) -> Deployment: ...


class Requirements(Protocol):
    def pending(self, kind: DeploymentRequirementKind) -> bool: ...
