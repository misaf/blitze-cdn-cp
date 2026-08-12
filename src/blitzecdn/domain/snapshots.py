from __future__ import annotations

import json
from typing import Any

from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.validation import STORED

#: Shape of the JSON in ``deployments.snapshot``: an object carrying the zones,
#: records, and derived sites a rollback converges.
SNAPSHOT_VERSION = 3


def encode_snapshot(
    domains: list[Domain], records: list[DnsRecord], sites: list[CdnSite]
) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    Version 3 stores only canonical state. ``sites`` remains an argument for
    source compatibility with callers and is deliberately ignored.
    """
    document: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "domains": [domain.model_dump(mode="json") for domain in domains],
        "records": [record.model_dump(mode="json") for record in records],
    }
    # Compatibility for databases created before records became canonical.
    # Production state with records never serializes the projection.
    if not records and sites:
        document["legacy_sites"] = [site.model_dump(mode="json") for site in sites]
    return json.dumps(document, sort_keys=True)


def decode_snapshot(snapshot: str) -> list[CdnSite]:
    """Return the sites a snapshot converges.

    Version 2 snapshots carried sites. Version 3 derives them from canonical
    records, removing the possibility of snapshotting contradictory truths.
    """
    document = _document(snapshot)
    if document.get("version") == 3:
        sites: dict[str, CdnSite] = {}
        for record in (
            DnsRecord.model_validate(item, context=STORED)
            for item in document.get("records", [])
        ):
            site = record.to_site()
            if site is not None:
                sites.setdefault(site.name, site)
        if sites:
            return list(sites.values())
        return [
            CdnSite.model_validate(item, context=STORED)
            for item in document.get("legacy_sites", [])
        ]
    return [
        CdnSite.model_validate(item, context=STORED)
        for item in document.get("sites", [])
    ]


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
    return data
