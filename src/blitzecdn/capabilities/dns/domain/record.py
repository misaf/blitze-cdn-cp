"""A record in a zone, and the site it routes to.

A record used to *be* a site: it carried the whole of ``SitePolicy`` and
``to_site`` turned it into one. It no longer does. A site is canonical and
lives in :mod:`blitzecdn.capabilities.sites.domain`; a record either answers
with an address of its own or names the site that answers for its hostname, and
``site`` is the whole of that relationship.

What this bought is one writer per fact. The old shape had none for two of
them: a hostname with an A and an AAAA record had two policies and two origins
for one virtual host, and the deriving code kept whichever it saw first. That
class of bug cannot be expressed here — both records name the same site, and
the site holds the single origin and the single policy.

This module still imports :mod:`blitzecdn.capabilities.sites` and never the
other way round. It imports a *name*, though, not a model: what a record needs
to know about a site is that it has one.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.validation import DNS_LABEL, SITE_NAME, hostname


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"


class DnsRecord(BaseModel):
    """One record in a zone: an address, or a route to a site.

    Exactly one of ``value`` and ``site`` is set, and which one is the CDN
    on/off switch.

    ``site`` set — the edge serves this hostname, and what DNS must answer with
    is an edge address rather than anything stored here. The origin the edge
    fetches from belongs to the site, along with every policy that used to sit
    on this row; ``type`` still says whether the published answer is the
    fleet's A or its AAAA address.

    ``value`` set — the record bypasses the CDN entirely and ``value`` is
    simply what DNS answers with. The record still belongs to us; the edge does
    not know the hostname exists.

    Turning the proxy off therefore means supplying the address DNS should
    answer with instead. That is one field more than the old ``proxied=False``
    and one guess fewer: the old switch left the origin address behind as the
    public answer, which published the origin to anyone who looked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    type: RecordType = RecordType.A
    value: str | None = None
    ttl: int = Field(default=300, ge=1, le=604800)
    site: str | None = None

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

    @field_validator("site")
    @classmethod
    def validate_site_name(cls, value: str | None) -> str | None:
        """Shape only. Whether the site *exists* is the service's question.

        A record is validated wherever one is constructed — decoding a
        snapshot, restoring a backup — and none of those places holds the site
        store. Checking existence here would make the model need one.
        """
        if value is None:
            return None
        normalized = value.strip().lower()
        if not SITE_NAME.fullmatch(normalized):
            raise ValueError(
                "site must start with a letter and contain only a-z, 0-9, and hyphens"
            )
        return normalized

    @model_validator(mode="after")
    def validate_address_or_site(self) -> Self:
        if (self.value is None) == (self.site is None):
            raise ValueError(
                "a record either answers with an address or routes to a site: "
                "set exactly one of 'value' and 'site'"
            )
        if self.value is None:
            return self
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

    @property
    def proxied(self) -> bool:
        """Whether the edge serves this hostname."""
        return self.site is not None


class RecordPatch(BaseModel):
    """A partial update to a record: every field optional, unset means untouched.

    Three fields now, where there used to be twenty. The policy went to
    ``SitePatch`` with the rest of the site.

    ``site`` is the one field where "unset" and "null" differ, and pydantic's
    ``exclude_unset`` is what tells them apart: omitting it leaves the routing
    alone, while sending it as ``null`` — together with a ``value`` — takes the
    hostname off the edge.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    site: str | None = None


__all__ = ["DnsRecord", "RecordPatch", "RecordType"]
