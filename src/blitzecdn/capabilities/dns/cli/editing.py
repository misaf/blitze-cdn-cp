"""What a command editing one zone's policy calls, whichever capability ships it.

Published to other capabilities' commands as `api.models` is to their routes.
"""

from __future__ import annotations

import typer

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.dns.domain import Domain, DomainPatch, PatchPolicy
from blitzecdn.entrypoints.cli import common

__all__ = ["report", "update"]


def update(name: str, patch: DomainPatch) -> Domain:
    return common.services().dns.update_domain(
        name, PatchPolicy.from_patch(patch), "cli"
    )


def report(zone: Domain, message: str, *, json_output: bool) -> None:
    """Print the zone, then say what it now does and whether it is served.

    Skipped under `--json`, which should not pay for the proxied-records query.
    """
    common.emit(serving_document(zone), json_output=json_output)
    if not json_output:
        typer.echo(_applied(zone, message))


def _applied(zone: Domain, message: str) -> str:
    """A confirmation that says whether anything in the zone is actually served yet."""
    if any(record.proxied for record in common.services().dns.list_records(zone.name)):
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} Nothing in {zone.name!r} is proxied yet, so nothing is "
        "served — put a hostname on the edge with 'blitzecdn record proxy'."
    )
