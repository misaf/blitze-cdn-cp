"""Running one scheduled job, and converging what it left owing.

Features own the jobs; this owns the rule that a run leaving a deployment
requirement pending is followed by a convergence.
"""

from __future__ import annotations

from blitzecdn.capabilities.maintenance.ports import Deployments, JobTable
from blitzecdn.core.exceptions import NotFoundError


class MaintenanceService:
    def __init__(
        self,
        *,
        jobs: JobTable,
        deployments: Deployments,
    ) -> None:
        self._jobs = jobs
        self._deployments = deployments

    def run(self, job: str, operator: str = "scheduler") -> None:
        """Run one named job, then converge if it left a requirement pending.

        An unknown name — a removed feature's queued job — is a ``NotFoundError``.
        """
        table = self._jobs
        if job not in table:
            known = ", ".join(sorted(table)) or "none"
            raise NotFoundError(
                f"no scheduled job named {job!r} is registered; installed: {known}"
            )
        table[job].run(operator)
        self._deployments.converge_requirements(operator)
