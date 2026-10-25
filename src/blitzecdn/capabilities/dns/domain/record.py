"""A record in a zone: what DNS answers, and when proxied, where the edge fetches.

A record holds no policy — the zone and its rules do. ``value`` does double
duty: unproxied it is the DNS answer, proxied it is the origin and DNS answers
with an edge address. Proxied A and AAAA records for one hostname must agree on
the origin.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.validation import DNS_LABEL, hostname


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"


class DnsRecord(BaseModel):
    """One record in a zone: an address, and whether the edge answers for it.

    ``value`` is always present, so publishing a private origin happens only when
    an operator unproxies with the address they want public. When proxied, ``type``
    says whether the fleet's A or AAAA address is published.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    type: RecordType = RecordType.A
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    #: Defaults to on, which is the reason an operator adds a record to a CDN
    #: at all, and matches what the dashboard people arrive from does.
    proxied: bool = True

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return hostname(value)

    @field_validator("name")
    @classmethod
    def validate_record_name(cls, value: str) -> str:
        """``@`` is the zone apex, ``*`` a wildcard, anything else a subdomain."""
        normalized = value.strip().lower().rstrip(".")
        if normalized in {"@", "*"}:
            return normalized
        if not normalized:
            raise ValueError("record name cannot be empty; use '@' for the apex")
        if not all(DNS_LABEL.fullmatch(label) for label in normalized.split(".")):
            raise ValueError(f"invalid record name: {value!r}")
        return normalized

    @model_validator(mode="after")
    def validate_address_matches_the_type(self) -> Self:
        try:
            address = ipaddress.ip_address(self.value.strip())
        except ValueError:
            raise ValueError(
                f"{self.type.value} record value must be an IP address"
            ) from None
        expected = 4 if self.type is RecordType.A else 6
        if address.version != expected:
            raise ValueError(
                f"{self.type.value} record value must be an IPv{expected} address"
            )
        return self

    @property
    def fqdn(self) -> str:
        """The hostname this record answers for."""
        if self.name == "@":
            return self.domain
        return f"{self.name}.{self.domain}"


class RecordPatch(BaseModel):
    """A partial update to a record; ``proxied`` and ``value`` change independently."""

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None


__all__ = ["DnsRecord", "RecordPatch", "RecordType"]
