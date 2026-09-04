"""The HTTP representations this capability publishes, and the bodies it takes.

A record validates by round-tripping through `dns.domain`: the published shape
carries no rule of its own, so the wire form and the domain form cannot drift
apart while sitting in different packages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from blitzecdn.api.models import Model
from blitzecdn.capabilities.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.dns.domain import Domain as DomainDomain
from blitzecdn.capabilities.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.dns.domain import RecordType as DomainRecordType


class Domain(Model):
    name: str

    @model_validator(mode="after")
    def valid_domain(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDomain:
        return DomainDomain.model_validate(self.model_dump())


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    def to_domain(self) -> DomainRecordType:
        return DomainRecordType(self.value)


class DnsRecord(Model):
    """A record: an address of its own, or the site that answers for it."""

    domain: str
    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str | None = None
    ttl: int = Field(default=300, ge=1, le=604800)
    site: str | None = None

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDnsRecord:
        return DomainDnsRecord.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainDnsRecord) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class RecordPatch(Model):
    """Send ``site`` as ``null`` together with a ``value`` to unroute a name."""

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    site: str | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


__all__ = ["DnsRecord", "Domain", "RecordPatch", "RecordType"]
