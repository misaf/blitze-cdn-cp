"""Request, response, and authentication state used by the HTTP adapter."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.api.v2_operations import PurgeEntry
from blitzecdn.core.validation import EDGE_LIMIT
from blitzecdn.features.certificates.domain import CERTIFICATE_RENEWAL_DAYS


class _RequestModel(BaseModel):
    """HTTP request bodies reject fields the current API does not define."""

    model_config = ConfigDict(extra="forbid")


class _FleetRequest(_RequestModel):
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class DeployRequest(_FleetRequest):
    check: bool = False


class DriftRequest(_FleetRequest):
    pass


class PurgeRequest(_FleetRequest):
    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: Empty the cache instead of removing named entries. Kept as its own flag
    #: rather than "no entries means everything" so a caller whose filter
    #: matched nothing cannot empty the fleet's cache by accident.
    purge_all: bool = False


class StatsRequest(_FleetRequest):
    pass


class OriginCheckRequest(_FleetRequest):
    """Which edges should answer. All of them, unless narrowed."""

    pass


class RenewRequest(_RequestModel):
    within_days: int = Field(default=CERTIFICATE_RENEWAL_DAYS, ge=0, le=3650)
    force: bool = False
    #: Narrow the run to these sites. None means every managed certificate;
    #: an unknown name is a 404 rather than a quiet no-op.
    sites: list[str] | None = Field(default=None, min_length=1)


class RollbackRequest(_RequestModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False
