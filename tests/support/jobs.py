"""Job handlers for tests that exercise the queue, not a real job kind."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ConfigDict

from blitzecdn.capabilities.jobs.domain import JobOutcome, JobPayload
from blitzecdn.capabilities.jobs.ports import JobHandler

__all__ = ["FreeFormPayload", "handling"]


class FreeFormPayload(JobPayload):
    """Any JSON object, for a kind that exists only in a test."""

    model_config = ConfigDict(extra="allow", frozen=True)


def handling(
    handlers: Mapping[str, Callable[[dict[str, Any]], JobOutcome]],
) -> tuple[JobHandler[Any], ...]:
    """Each test handler as a job handler over its kind's free-form payload."""

    def handler(
        kind: str, handle: Callable[[dict[str, Any]], JobOutcome]
    ) -> JobHandler[FreeFormPayload]:
        model = type(f"FreeForm[{kind}]", (FreeFormPayload,), {"kind": kind})
        return JobHandler(
            model, lambda payload: handle(dict(payload.model_extra or {}))
        )

    return tuple(handler(kind, handle) for kind, handle in handlers.items())
