from __future__ import annotations

import hashlib
import json
from typing import Any

from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.sites import CdnSite


def encode_snapshot(domains: list[Domain], records: list[DnsRecord]) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    Records are the only canonical state here. Sites are derived from them on
    read rather than stored alongside, so a snapshot cannot carry two truths
    that disagree.
    """
    return json.dumps(
        {
            "domains": [domain.model_dump(mode="json") for domain in domains],
            "records": [record.model_dump(mode="json") for record in records],
        },
        sort_keys=True,
    )


def snapshot_digest(snapshot: str) -> str:
    """A short stable identity for a snapshot, for comparing two of them.

    A rollback records the canonical state it started from so it can refuse to
    adopt over a change made while it was running. Storing the digest rather
    than a second copy of the snapshot keeps the row the size it was:
    ``encode_snapshot`` sorts its keys, so equal desired state always produces
    equal bytes and therefore an equal digest.
    """
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def decode_snapshot(snapshot: str) -> list[CdnSite]:
    """Return the sites a snapshot converges, derived from its records."""
    document = _document(snapshot)
    sites: dict[str, CdnSite] = {}
    for record in (
        DnsRecord.model_validate(item) for item in document.get("records", [])
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
        [DnsRecord.model_validate(item) for item in data.get("records", [])],
    )


def _document(snapshot: str) -> dict[str, Any]:
    data = json.loads(snapshot)
    if not isinstance(data, dict):
        raise ValueError("deployment snapshot is not an object")
    expected = {"domains", "records"}
    if set(data) != expected:
        raise ValueError(
            "deployment snapshot must contain exactly 'domains' and 'records'"
        )
    if not isinstance(data["domains"], list) or not isinstance(data["records"], list):
        raise ValueError("deployment snapshot domains and records must be lists")
    return data
