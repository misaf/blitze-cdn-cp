"""What BlitzeCDN does about public DNS: nothing, stated as a value.

Zones and records are stored, never published — no nameserver, provider client
or `nsupdate`. ``blitzecdn domain export``, ``doctor`` and every command that
changes what DNS should answer report this, rather than claiming what DNS does.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.sites.domain.record import DnsRecord, RecordType

__all__ = [
    "PUBLICATION",
    "DnsExport",
    "DnsPublication",
    "ExportedRecord",
    "PublicationMode",
]


class PublicationMode(StrEnum):
    """Who publishes the records this control plane holds."""

    #: Nobody here; something outside reads the records and acts. An enum, so a
    #: publishing mode can be added as a member.
    EXTERNAL = "external"


class DnsPublication(BaseModel):
    """Whether this installation publishes DNS, and what it does instead."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PublicationMode
    #: Whether a record written here becomes a public answer unaided: never, today.
    publishes: bool
    detail: str


#: The single answer.
PUBLICATION = DnsPublication(
    mode=PublicationMode.EXTERNAL,
    publishes=False,
    detail=(
        "BlitzeCDN does not publish DNS. Records here are desired state: a "
        "proxied hostname must be answered with an edge address, and an "
        "unproxied one with the record's own value. Feed 'blitzecdn domain "
        "export' to whatever is authoritative for these zones, or a change "
        "made here reaches no visitor."
    ),
)


#: What a proxied record asks DNS to answer with: an address only the fleet knows.
EDGE_ANSWER = "an edge address"


class ExportedRecord(BaseModel):
    """One record as whatever is authoritative for its zone should publish it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fqdn: str
    domain: str
    name: str
    type: RecordType
    ttl: int
    proxied: bool
    #: What DNS should answer with: the record's value when unproxied, and
    #: ``EDGE_ANSWER`` when proxied, since the edge address is the fleet's.
    answer: str
    #: The record's own value, only when it *is* the public answer. A proxied
    #: record's value is the origin the edge fetches from and never leaves here.
    value: str | None

    @classmethod
    def from_record(cls, record: DnsRecord) -> ExportedRecord:
        return cls(
            fqdn=record.fqdn,
            domain=record.domain,
            name=record.name,
            type=record.type,
            ttl=record.ttl,
            proxied=record.proxied,
            answer=EDGE_ANSWER if record.proxied else record.value,
            value=None if record.proxied else record.value,
        )


class DnsExport(BaseModel):
    """Desired DNS state, with ``publication`` saying nothing here publishes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    publication: DnsPublication
    records: tuple[ExportedRecord, ...]
