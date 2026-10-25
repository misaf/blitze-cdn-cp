"""The slice of the rest of the control plane a maintenance run actually uses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from blitzecdn.capabilities.deployments.domain import Deployment
from blitzecdn.core.plugins import ScheduledJob

__all__ = ["Deployments", "JobTable"]


#: Every scheduled job the installed plugins contribute, by name.
type JobTable = Mapping[str, ScheduledJob]


class Deployments(Protocol):
    def converge_requirements(self, operator: str) -> Deployment | None: ...
