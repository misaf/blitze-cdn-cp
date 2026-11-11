"""What a command editing one zone's policy calls, whichever capability ships it.

Zone writes go through the sites service, which the command injects as
:data:`Sites`; output uses its public projection.
"""

from __future__ import annotations

import typer
from dishka import FromDishka

from blitzecdn.capabilities.sites import (
    Domain,
    DomainPatch,
    PatchPolicy,
    SiteService,
    serving_document,
)
from blitzecdn.delivery.cli import common

__all__ = ["Sites", "report", "update"]

#: The parameter a zone-editing command declares to receive the sites service;
#: spelled here so a capability's commands need not import the sites capability.
Sites = FromDishka[SiteService]


def update(sites: SiteService, name: str, patch: DomainPatch) -> Domain:
    return sites.update_domain(name, PatchPolicy.from_patch(patch), "cli")


def report(
    sites: SiteService, zone: Domain, message: str, *, json_output: bool
) -> None:
    """Print the zone, then say what it now does and whether it is served.

    Skipped under `--json`, which should not pay for the proxied-records query.
    """
    common.emit(serving_document(zone), json_output=json_output)
    if not json_output:
        typer.echo(_applied(sites, zone, message))


def _applied(sites: SiteService, zone: Domain, message: str) -> str:
    """A confirmation that says whether anything in the zone is actually served yet."""
    if any(record.proxied for record in sites.list_records(zone.name)):
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} Nothing in {zone.name!r} is proxied yet, so nothing is "
        "served — put a hostname on the edge with 'blitzecdn record proxy'."
    )
