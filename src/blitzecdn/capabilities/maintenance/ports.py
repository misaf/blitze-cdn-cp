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


#: Every scheduled job the installed plugins contribute, by name.
type JobTable = Mapping[str, ScheduledJob]


class Deployments(Protocol):
    def submit_deployment(self, operator: str) -> Deployment: ...


class Requirements(Protocol):
    def pending(self, kind: DeploymentRequirementKind) -> bool: ...
