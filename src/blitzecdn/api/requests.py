"""The request vocabulary core owns.

The mirror of :mod:`blitzecdn.api.models`: what every request body is built
from, and the one body shape more than one capability accepts. A deploy, a
drift check, a purge and an origin check all take a host limit, so
`FleetRequest` is core's; `DeployRequest` and `RollbackRequest` are
`deployments`' and live with the routes that accept them.
"""

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
