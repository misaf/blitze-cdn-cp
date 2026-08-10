from __future__ import annotations

import json
from typing import Any

from blitzecdn.domain.models import STORED, CdnSite, DnsRecord, Domain

#: Shape of the JSON in ``deployments.snapshot``: an object carrying the zones,
#: records, and derived sites a rollback converges.
SNAPSHOT_VERSION = 2


def encode_snapshot(
    domains: list[Domain], records: list[DnsRecord], sites: list[CdnSite]
) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    Records are the source of truth, so they are what a snapshot carries; sites
    are derived and are included only so a rollback can converge without
    re-deriving.
    """
    return json.dumps(
        {
            "version": SNAPSHOT_VERSION,
            "domains": [domain.model_dump(mode="json") for domain in domains],
            "records": [record.model_dump(mode="json") for record in records],
            "sites": [site.model_dump(mode="json") for site in sites],
        },
        sort_keys=True,
    )


def decode_snapshot(snapshot: str) -> list[CdnSite]:
    """Return the sites a snapshot converges.

    Sites are derived from records; a snapshot carries them anyway so a
    rollback can converge without re-deriving first.
    """
    return [
        CdnSite.model_validate(item, context=STORED)
        for item in _document(snapshot).get("sites", [])
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
