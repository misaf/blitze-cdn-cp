"""Request bodies shared by every version of the HTTP API.

These describe the *input* to an operation — deploy, drift, purge, stats,
origin check, renew, rollback — and every published version has always accepted
the identical body. See :mod:`blitzecdn.api.operations` for why that makes them
one definition rather than one per version, and for how a version that needs to
diverge does so without renaming the other version's published schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.validation import EDGE_LIMIT


class RequestModel(BaseModel):
    """HTTP request bodies reject fields the current API does not define."""

    model_config = ConfigDict(extra="forbid")


class FleetRequest(RequestModel):
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class DeployRequest(FleetRequest):
    check: bool = False


class DriftRequest(FleetRequest):
    pass


class RollbackRequest(RequestModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False
