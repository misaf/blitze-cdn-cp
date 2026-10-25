"""What BlitzeCDN does about public DNS, stated where an operator meets it.

BlitzeCDN records zones and records, and it does not publish them. There is no
authoritative nameserver here, no provider client, no zone transfer, no
`nsupdate`: `blitzecdn record add` writes a row, and the world learns nothing
about it until whatever *does* publish DNS is told.

A message such as "cdn.example.com now bypasses the CDN and answers with
203.0.113.9" would be a claim about what DNS is doing, made by the one
component that cannot make it. So the limitation is a value, reported by
`blitzecdn dns export`, by `blitzecdn doctor`, and beside every command that
changes what DNS *should* answer. A capability that publishes DNS can be
installed later — the export is already shaped as its input — and this says
exactly what such a capability would have to take over.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["PUBLICATION", "DnsPublication", "PublicationMode"]


class PublicationMode(StrEnum):
    """Who publishes the records this control plane holds."""

    #: Nobody here. The records are desired state that something outside this
    #: control plane has to read and act on. This is the only mode there is,
    #: and it is an enum rather than a boolean so that installing a capability
    #: which does publish is a new member rather than a new field.
    EXTERNAL = "external"


class DnsPublication(BaseModel):
    """Whether this installation publishes DNS, and what it does instead."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: PublicationMode
    #: Whether a record written here becomes a public answer without further
    #: action. Always false today, and named plainly rather than left for a
    #: reader to infer from ``mode``.
    publishes: bool
    detail: str


#: The single instance, because there is a single answer. A function returning
#: it would suggest the answer could depend on something, and today nothing
#: about an installation changes it.
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
