"""The slice of the rest of the control plane a maintenance run actually uses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
)
from blitzecdn.core.plugins import ScheduledJob

__all__ = ["Deployments", "JobTable", "Requirements"]


class JobTable(Protocol):
    """Every scheduled job the installed plugins contribute, by name.

    A callable rather than a mapping because the table is resolved from the
    composition root and this service is one of the things that root builds:
    asking for it at construction time would be asking for a list that includes
    a job contributed by a plugin that has not been given this service yet.
    Called once per run, which is once per worker process.
    """

    def __call__(self) -> Mapping[str, ScheduledJob]: ...


class Deployments(Protocol):
    def submit_deployment(self, operator: str) -> Deployment: ...


class Requirements(Protocol):
    def pending(self, kind: DeploymentRequirementKind) -> bool: ...
