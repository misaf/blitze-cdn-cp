"""The cache operations' HTTP requests. Reports are the queued operation's result."""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field, field_validator

from blitzecdn.capabilities.cache.domain import PurgeEntry as DomainPurgeEntry
from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.entrypoints.api.models import Model
from blitzecdn.entrypoints.api.requests import FleetRequest


class PurgeEntry(Model):
    host: str
    uri: str
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host", "uri")
    @classmethod
    def valid_entry_field(cls, value: str, info: Any) -> str:
        payload = {"host": "example.com", "uri": "/", info.field_name: value}
        return cast(
            "str", getattr(DomainPurgeEntry.model_validate(payload), info.field_name)
        )

    def to_domain(self) -> DomainPurgeEntry:
        return DomainPurgeEntry.model_validate(self.model_dump())


class PurgeRequest(FleetRequest):
    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: A separate flag, so a filter matching nothing never empties the cache.
    purge_all: bool = False


class StatsRequest(FleetRequest):
    pass


__all__ = ["PurgeEntry", "PurgeRequest", "StatsRequest"]
