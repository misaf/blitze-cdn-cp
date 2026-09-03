"""Running one scheduled job, and paying off what it left owing.

Every scheduled job is contributed by the plugin that owns the work — renewing
certificates belongs to `certificates`, checking drift to `deployments` — so
this service does not know what any of them do. What it owns is the rule that
holds across all of them: a maintenance run that changed something a fleet has
not seen yet must be followed by a convergence, or the change sits in the
database until an operator happens to deploy for another reason.
"""

from __future__ import annotations

from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
from blitzecdn.capabilities.maintenance.ports import Deployments, JobTable, Requirements


class MaintenanceService:
    def __init__(
        self,
        *,
        jobs: JobTable,
        deployments: Deployments,
        requirements: Requirements,
    ) -> None:
        self._jobs = jobs
        self._deployments = deployments
        self._requirements = requirements

    def run(self, job: str, operator: str = "scheduler") -> None:
        """Run one named job, then converge if it left a requirement pending.

        The name arrived in a queue message, so it may name a job that no
        longer exists — a plugin uninstalled while its last message was still
        in flight. That is a `NotFoundError` naming what is installed rather
        than a `KeyError`, because the operator reading the worker log is the
        person who uninstalled it.
        """
        table = self._jobs()
        if job not in table:
            known = ", ".join(sorted(table)) or "none"
            raise NotFoundError(
                f"no scheduled job named {job!r} is registered; installed: {known}"
            )
        table[job].run(operator)
        if self._requirements.pending(DeploymentRequirementKind.CERTIFICATES):
            self._deployments.submit_deployment(operator)
