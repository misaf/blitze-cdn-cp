"""The desired state a deployment converges, and can be rolled back to.

A snapshot is the whole of canonical desired state at one instant: the zones,
their records, and the sites those records route to. A deployment records one
and converges it; a rollback reads an older one back.

The schema version is written down because a snapshot outlives the run that
made it — an older successful deployment is a rollback target for as long as it
is in the history table. It is a discriminator, not a compatibility layer:
there is exactly one version, and a document that is not it is refused rather
than guessed at.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.capabilities.sites.domain import CdnSite

SNAPSHOT_SCHEMA_VERSION = 1

_SECTIONS = ("domains", "records", "sites")


def encode_snapshot(
    domains: list[Domain], records: list[DnsRecord], sites: list[CdnSite]
) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    All three, because all three are canonical. Sites are written down rather
    than derived from the records on read: a site no record routes to yet is
    desired state that no record mentions.
    """
    return json.dumps(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "domains": [domain.model_dump(mode="json") for domain in domains],
            "records": [record.model_dump(mode="json") for record in records],
            "sites": [site.model_dump(mode="json") for site in sites],
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
    """Return the sites a snapshot converges."""
    return decode_snapshot_state(snapshot)[2]


def decode_snapshot_state(
    snapshot: str,
) -> tuple[list[Domain], list[DnsRecord], list[CdnSite]]:
    """Return everything a rollback restores: zones, records, and sites."""
    document = _document(snapshot)
    return (
        [Domain.model_validate(item) for item in document["domains"]],
        [DnsRecord.model_validate(item) for item in document["records"]],
        [CdnSite.model_validate(item) for item in document["sites"]],
    )


def _document(snapshot: str) -> dict[str, Any]:
    data = json.loads(snapshot)
    if not isinstance(data, dict):
        raise ValueError("deployment snapshot is not an object")
    if set(data) != {"schema_version", *_SECTIONS}:
        raise ValueError(
            "deployment snapshot must contain a schema version, " + ", ".join(_SECTIONS)
        )
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("deployment snapshot schema version must be an integer")
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(f"unsupported deployment snapshot schema version: {version}")
    for key in _SECTIONS:
        if not isinstance(data[key], list):
            raise ValueError(f"deployment snapshot {key} must be a list")
    return data


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "decode_snapshot",
    "decode_snapshot_state",
    "encode_snapshot",
    "snapshot_digest",
]
