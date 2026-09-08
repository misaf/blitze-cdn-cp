"""The desired state a deployment converges, and can be rolled back to.

A snapshot is the whole of canonical desired state at one instant: the zones,
the rules that override their policy, and the records in them. A deployment
records one and converges it; a rollback reads an older one back.

There is no ``sites`` section and there is deliberately no place to add one.
The virtual hosts an edge serves are derived from those three, so writing them
down would put a second, older answer beside the state they come from — and a
rollback restoring both would be restoring a document that could disagree with
itself. ``decode_snapshot`` derives them on the way out instead, which is why
an old snapshot converges to what the *current* derivation makes of it.

The rules are in it for a sharper reason: a rollback deletes the zone rows and
writes them again, and a rule is keyed to its zone with ON DELETE CASCADE. A
snapshot that did not carry them would not merely fail to restore them — it
would take every rule in the installation with it.

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

from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    Rule,
    derive_hosts,
)

SNAPSHOT_SCHEMA_VERSION = 1

_SECTIONS = ("domains", "records", "rules")


def encode_snapshot(
    domains: list[Domain], records: list[DnsRecord], rules: list[Rule]
) -> str:
    """Serialise the desired state a deployment converges and can roll back to.

    Three sections, because three things are canonical. What an edge serves is
    not among them; see the module docstring.
    """
    return json.dumps(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "domains": [domain.model_dump(mode="json") for domain in domains],
            "records": [record.model_dump(mode="json") for record in records],
            "rules": [rule.model_dump(mode="json") for rule in rules],
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
    """The virtual hosts a snapshot converges, derived from what it holds."""
    domains, records, rules = decode_snapshot_state(snapshot)
    return derive_hosts(domains, rules, records)


def decode_snapshot_state(
    snapshot: str,
) -> tuple[list[Domain], list[DnsRecord], list[Rule]]:
    """Everything a rollback restores: zones, records, and rules."""
    document = _document(snapshot)
    return (
        [Domain.model_validate(item) for item in document["domains"]],
        [DnsRecord.model_validate(item) for item in document["records"]],
        [Rule.model_validate(item) for item in document["rules"]],
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
