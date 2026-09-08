"""A record in a zone: what DNS answers, and, when proxied, where the edge fetches from.

A record carries no serving policy and names no object that does: the zone
says how a hostname is served, and a rule says how this hostname differs from
the rest of the zone. What a record does own is its address — ``value`` — and
that address does double duty. Unproxied, it *is* the DNS answer. Proxied, it
is the origin the edge connects to, and DNS answers with an edge address
instead. One field, because that is the Cloudflare shape: the record's
``content`` is the origin while the orange cloud is on and the public answer
the moment it goes off.

The three shapes this has had are worth keeping straight, because each fixed
the one before it. First a record *was* a site: it carried the whole of
``SitePolicy``, and a hostname with an A and an AAAA record therefore had two
policies and two origins for one virtual host, with the deriving code keeping
whichever it saw first. Then it named a canonical site, which fixed that but
made publishing a hostname a two-object job. Then it said ``proxied`` and
carried no address at all, with the origin living on the zone or a rule —
which split a hostname's origin from its address and forced one shared origin
per zone. Now it carries its own address again, and ``proxied`` is the on/off
switch and nothing more. A proxied A and AAAA still cannot disagree about
policy — neither holds any — but they must agree about where the edge fetches
from, and validation says so.
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

    ``value`` is the address: what DNS answers with when ``proxied`` is off,
    and where the edge fetches from when it is on. It is always present — the
    same shape as a Cloudflare record, where ``content`` holds the origin while
    the orange cloud is on and becomes the public answer when it stops.
    Publishing a private origin is therefore the operator's own choice at
    unproxying time, made with the address they want public; it is not an
    accident that happens whenever the switch flips.

    ``proxied=True`` — the edge serves this hostname. What DNS answers with is
    an edge address, which the fleet supplies rather than anything stored here,
    and how the hostname is served comes from the zone's policy and whichever
    rule matches it. ``type`` still says whether the published answer is the
    fleet's A or its AAAA address, and ``value`` says which origin the edge
    connects to.

    ``proxied=False`` — the record bypasses the CDN entirely and ``value`` is
    simply what DNS answers with. The record still belongs to us; the edge does
    not know the hostname exists.
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
    """A partial update to a record: every field optional, unset means untouched.

    ``proxied`` and ``value`` are independent because a record always carries a
    value: flipping the orange cloud does not change the address, it changes
    what the address means. The edge starts (or stops) serving a hostname on
    the next deploy, and DNS answers with a fleet address (or with ``value``)
    once the publisher picks it up.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None


__all__ = ["DnsRecord", "RecordPatch", "RecordType"]
