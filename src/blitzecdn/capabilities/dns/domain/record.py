"""A record in a zone: an address of its own, or a hostname on the edge.

A record carries no policy and names no object that does. ``proxied`` says
whether the edge serves this hostname; the zone says how, and a rule says how
this hostname differs from the rest of the zone. That is the whole of it.

The three shapes this has had are worth keeping straight, because each fixed
the one before it. First a record *was* a site: it carried the whole of
``SitePolicy``, and a hostname with an A and an AAAA record therefore had two
policies and two origins for one virtual host, with the deriving code keeping
whichever it saw first. Then it named a canonical site, which fixed that but
made publishing a hostname a two-object job. Now it says ``proxied`` and the
policy lives on the zone — one object for the common case, and the A and the
AAAA still cannot disagree, because neither of them holds a policy to disagree
with.
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
    """One record in a zone: an address of its own, or a hostname on the edge.

    ``proxied`` is the CDN on/off switch and ``value`` is what DNS answers
    with, and the two are tied together: a proxied record has no ``value`` and
    an unproxied one must have one.

    ``proxied=True`` — the edge serves this hostname. What DNS answers with is
    an edge address, which the fleet supplies rather than anything stored here,
    and how the hostname is served comes from the zone's policy and whichever
    rule matches it. ``type`` still says whether the published answer is the
    fleet's A or its AAAA address.

    ``proxied=False`` — the record bypasses the CDN entirely and ``value`` is
    simply what DNS answers with. The record still belongs to us; the edge does
    not know the hostname exists.

    Requiring the address when the proxy goes off is the one place this
    deliberately parts company with Cloudflare, where ``content`` holds the
    origin while proxying and becomes the public answer when it stops — which
    publishes the origin address to anyone who looks, at the moment an operator
    is least expecting it. There is no address the control plane could
    substitute here that is not either that leak or a black hole, so it asks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    type: RecordType = RecordType.A
    value: str | None = None
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
    def validate_address_matches_the_switch(self) -> Self:
        if self.proxied and self.value is not None:
            raise ValueError(
                "a proxied record is answered with an edge address, so it "
                "cannot carry a 'value' of its own; set proxied=false to "
                "answer with your own address instead"
            )
        if not self.proxied and self.value is None:
            raise ValueError(
                "an unproxied record is what DNS answers with, so it needs a "
                "'value'; supply the address this hostname should resolve to"
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


class RecordPatch(BaseModel):
    """A partial update to a record: every field optional, unset means untouched.

    Three fields now, where there used to be twenty. The policy went to the
    zone with the rest of what a site used to hold.

    Taking a hostname off the edge is ``{"proxied": false, "value": ...}`` in
    one request, because neither half is a valid record on its own — see
    ``DnsRecord``. Putting it back on is ``{"proxied": true, "value": null}``,
    and ``value`` is the field where "unset" and "null" differ: pydantic's
    ``exclude_unset`` is what tells "leave the address alone" from "clear it".
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None


__all__ = ["DnsRecord", "RecordPatch", "RecordType"]
