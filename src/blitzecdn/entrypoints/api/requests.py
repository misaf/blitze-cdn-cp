"""The request frame, and the host-limit body several capabilities take."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.domain.validation import EDGE_LIMIT


class RequestModel(BaseModel):
    """HTTP request bodies reject fields the current API does not define."""

    model_config = ConfigDict(extra="forbid")


class FleetRequest(RequestModel):
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


__all__ = ["FleetRequest", "RequestModel"]
