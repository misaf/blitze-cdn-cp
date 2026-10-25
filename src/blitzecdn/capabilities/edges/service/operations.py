"""Removing an edge as a queued operation: its kind, payload and run.

The HTTP API queues it for the worker, one per edge at a time; the CLI calls
the service directly. The result says whether the host was torn down and what
each host reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from blitzecdn.capabilities.edges.service.fleet import EdgeOperationsService

__all__ = [
    "OPERATION_KINDS",
    "REMOVE_OPERATION",
    "operation_handlers",
    "remove_dedupe_key",
    "remove_payload",
]

REMOVE_OPERATION = "edges.remove"
OPERATION_KINDS = (REMOVE_OPERATION,)


def remove_payload(
    operator: str, *, name: str, decommission: bool, force: bool
) -> dict[str, Any]:
    return {
        "operator": operator,
        "name": name,
        "decommission": decommission,
        "force": force,
    }


def remove_dedupe_key(name: str) -> str:
    """One removal per edge in flight: a second would race the first's teardown."""
    return f"{REMOVE_OPERATION}:{name}"


def operation_handlers(
    edges: EdgeOperationsService,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """The removal's run, from its payload to its JSON result."""

    def remove(payload: Mapping[str, Any]) -> dict[str, Any]:
        operator = str(payload["operator"])
        name = str(payload["name"])
        if not payload["decommission"]:
            edges.remove_edge(name, operator)
            return {"name": name, "decommissioned": False, "hosts": []}
        hosts = edges.decommission_edge(name, operator, force=bool(payload["force"]))
        return {
            "name": name,
            "decommissioned": True,
            "hosts": [host.model_dump(mode="json") for host in hosts],
        }

    return {REMOVE_OPERATION: remove}
