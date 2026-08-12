from __future__ import annotations

import json
from typing import Any

from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.validation import STORED

#: Shape of the JSON in ``deployments.snapshot``: an object carrying the zones
#: and records a rollback converges.
#:
#: Written on every snapshot and checked on every read. It is not a
#: compatibility shim — nothing decodes an older shape — but the thing that
#: makes a future format change fail loudly on the snapshot rather than
#: silently converge a fleet from a document this code misread.
SNAPSHOT_VERSION = 3


def encode_snapshot(domains: list[Domain], records: list[DnsRecord]) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    Records are the only canonical state here. Sites are derived from them on
    read rather than stored alongside, so a snapshot cannot carry two truths
    that disagree.
    """
    return json.dumps(
        {
            "version": SNAPSHOT_VERSION,
            "domains": [domain.model_dump(mode="json") for domain in domains],
            "records": [record.model_dump(mode="json") for record in records],
        },
        sort_keys=True,
    )


def decode_snapshot(snapshot: str) -> list[CdnSite]:
    """Return the sites a snapshot converges, derived from its records."""
    document = _document(snapshot)
    sites: dict[str, CdnSite] = {}
    for record in (
        DnsRecord.model_validate(item, context=STORED)
        for item in document.get("records", [])
    ):
        site = record.to_site()
        if site is not None:
            sites.setdefault(site.name, site)
    return list(sites.values())


def decode_snapshot_zones(snapshot: str) -> tuple[list[Domain], list[DnsRecord]]:
    """Return the zones a snapshot carries, which a rollback re-derives from."""
    data = _document(snapshot)
    return (
        [Domain.model_validate(item) for item in data.get("domains", [])],
        [
            DnsRecord.model_validate(item, context=STORED)
            for item in data.get("records", [])
        ],
    )


def _document(snapshot: str) -> dict[str, Any]:
    data = json.loads(snapshot)
    if not isinstance(data, dict):
        raise ValueError("deployment snapshot is not an object")
    version = data.get("version")
    if version != SNAPSHOT_VERSION:
        raise ValueError(
            f"deployment snapshot is version {version!r}, "
            f"but this release reads version {SNAPSHOT_VERSION}"
        )
    return data
