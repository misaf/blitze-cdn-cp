"""The canonical state a release is compiled from, as one addressable value.

Three sections, because three things are canonical: the zones that hold policy,
the rules that override it, and the records that say which hostnames the edges
answer for. What an edge is asked to serve is not among them — virtual hosts
are derived, and writing them down here would put a second, older answer beside
the state they come from. See
``docs/decisions/0001-zone-policy-and-composition.md``.

The rules are in it for a sharper reason than symmetry. A rollback deletes the
zone rows and writes them again, and a rule is keyed to its zone with ON DELETE
CASCADE — so inputs that did not carry rules would not merely fail to restore
them, they would take every rule in the installation with them.

This is a typed value rather than the JSON string it used to be. The string had
to be parsed at four call sites, each of which re-derived what the sections
were; the digest was a free function beside it, and nothing stopped a caller
digesting a document that had never been through the parser. Here the parse
happens once, at the edge of the system, and everything downstream holds
something that cannot be malformed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule

__all__ = ["INPUTS_SCHEMA_VERSION", "ReleaseInputs"]

#: A discriminator, not a compatibility layer. Inputs outlive the run that
#: compiled them — an older successful deployment is a rollback target for as
#: long as it is in the history table — so a document arriving from storage
#: says which shape it is. There is exactly one, and a document that is not it
#: is refused rather than guessed at.
INPUTS_SCHEMA_VERSION = 1


class ReleaseInputs(BaseModel):
    """Every canonical fact a compilation reads, and nothing else.

    Frozen, because a release is addressed by the digest of these bytes: a
    value that could be edited after it was digested is a value whose digest
    means nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=INPUTS_SCHEMA_VERSION)
    domains: tuple[Domain, ...] = ()
    records: tuple[DnsRecord, ...] = ()
    rules: tuple[Rule, ...] = ()

    @classmethod
    def of(
        cls,
        domains: list[Domain],
        records: list[DnsRecord],
        rules: list[Rule],
    ) -> Self:
        """The state as the stores hand it over, put into canonical order.

        Sorted here rather than trusted from the caller. The stores do return
        rows in a deterministic order, so this changes no digest in practice —
        but "in practice" is the wrong footing for a value whose whole purpose
        is that equal state produces equal bytes. A restore that writes rows
        back in insertion order, a backup read out of a different engine, or a
        second store added later would each be free to hand over the same
        state in a different order, and the release would then be a different
        release for no reason an operator could see.

        Rules sort by ``order``, which is ``(priority, name)`` — the same key
        ``resolve_policy`` sorts by before deciding which one wins. Sorting by
        anything else here would put the canonical form out of step with the
        semantics, which is a worse defect than not sorting at all.
        """
        return cls(
            domains=tuple(sorted(domains, key=lambda zone: zone.name)),
            records=tuple(
                sorted(
                    records,
                    key=lambda record: (record.domain, record.name, record.type.value),
                )
            ),
            rules=tuple(sorted(rules, key=lambda rule: (rule.domain, *rule.order))),
        )

    def encode(self) -> str:
        """The canonical bytes of this state: sorted keys, stable ordering.

        Two equal states must produce equal bytes, or the digest below is a
        random number. ``sort_keys`` settles the key order; the sequences keep
        the order the stores return, which is itself deterministic — zones and
        rules come back ordered, and a rule's position is its priority.
        """
        return json.dumps(self.model_dump(mode="json"), sort_keys=True)

    @property
    def digest(self) -> str:
        """A stable identity for this state, for comparing two of them.

        A rollback records the canonical state it started from so it can refuse
        to adopt over a change made while it was converging. Storing the digest
        rather than a second copy keeps the row the size it was.
        """
        return hashlib.sha256(self.encode().encode("utf-8")).hexdigest()

    @classmethod
    def decode(cls, document: str) -> Self:
        """Read stored inputs back, refusing anything that is not this shape."""
        try:
            data: Any = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ValueError(f"release inputs are not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("release inputs are not an object")
        version = data.get("schema_version")
        # Checked before validation so the message names the version rather
        # than the twelve fields a future shape happens to differ in.
        if version != INPUTS_SCHEMA_VERSION:
            raise ValueError(f"unsupported release inputs schema version: {version!r}")
        return cls.model_validate(data)
