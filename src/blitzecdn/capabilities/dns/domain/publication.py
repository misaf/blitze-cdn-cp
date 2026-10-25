"""What BlitzeCDN does about public DNS: nothing, stated as a value.

Zones and records are stored, never published — no nameserver, provider client
or `nsupdate`. ``blitzecdn dns export``, ``doctor`` and every command that
changes what DNS should answer report this, rather than claiming what DNS does.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["PUBLICATION", "DnsPublication", "PublicationMode"]


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
        "unproxied one with the record's own value. Feed 'blitzecdn dns "
        "export' to whatever is authoritative for these zones, or a change "
        "made here reaches no visitor."
    ),
)
