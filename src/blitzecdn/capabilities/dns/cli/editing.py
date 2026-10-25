"""What a command editing one zone's policy calls, whichever capability ships it.

`blitzecdn domain cache` belongs to `cache` and `domain ssl` to `tls`, because
each edits its own capability's contract, but every one of them changes the
zone through dns and ends by saying what the zone now does. This module is
that shared part, published to the other capabilities' commands the way
`api.models` is published to their routes.
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

    Every policy command ends this way, so it ends here rather than in each of
    them. Not `common.emit(note=...)`: composing the note means asking the
    control plane whether anything in the zone is proxied, and a `--json`
    caller should not pay for a query whose only output is a sentence it
    discards.
    """
    common.emit(serving_document(zone), json_output=json_output)
    if not json_output:
        typer.echo(_applied(zone, message))


def _applied(zone: Domain, message: str) -> str:
    """A confirmation that says whether anything is actually serving yet.

    A zone's policy applies to the hostnames in it that are proxied, so a zone
    with none is configuration with nothing behind it. Saying so here is what
    stops an operator tuning cache headers on a zone that serves no traffic and
    wondering why nothing changed.
    """
    if any(record.proxied for record in common.services().dns.list_records(zone.name)):
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} Nothing in {zone.name!r} is proxied yet, so nothing is "
        "served — put a hostname on the edge with 'blitzecdn record proxy'."
    )
