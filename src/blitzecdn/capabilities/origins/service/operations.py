"""The fleet origin check as a queued operation: its kind, payload and run.

The HTTP API queues it for the worker; the CLI calls the service directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from blitzecdn.capabilities.origins.service.checks import OriginCheckService

__all__ = [
    "CHECK_OPERATION",
    "OPERATION_KINDS",
    "check_payload",
    "operation_handlers",
]

CHECK_OPERATION = "origins.check"
OPERATION_KINDS = (CHECK_OPERATION,)


def check_payload(operator: str, *, host_limit: str | None) -> dict[str, Any]:
    return {"operator": operator, "host_limit": host_limit}


def operation_handlers(
    origins: OriginCheckService,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """The check's run, from its payload to its JSON report."""

    def check(payload: Mapping[str, Any]) -> dict[str, Any]:
        report = origins.check_origins(
            str(payload["operator"]), host_limit=payload.get("host_limit")
        )
        return report.model_dump(mode="json")

    return {CHECK_OPERATION: check}
