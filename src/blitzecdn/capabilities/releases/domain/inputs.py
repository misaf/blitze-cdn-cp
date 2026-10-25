"""The canonical state a release is compiled from, as one addressable value.

Zones, rules, records and fleet settings — never derived hosts. Rules are
included because a rollback rewrites zones and rules cascade from them;
settings, so a rollback reproduces the edge's ciphers, timeouts and logging.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule

__all__ = ["INPUTS_SCHEMA_VERSION", "ReleaseInputs"]

#: A discriminator for stored documents; there is one shape, and anything
#: else is refused.
INPUTS_SCHEMA_VERSION = 4


class ReleaseInputs(BaseModel):
    """Every canonical fact a compilation reads; frozen, since its digest names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=INPUTS_SCHEMA_VERSION)
    domains: tuple[Domain, ...] = ()
    records: tuple[DnsRecord, ...] = ()
    rules: tuple[Rule, ...] = ()
    #: Fleet-wide Ansible settings by name, as `blitzecdn config set` stored
    #: them. Non-secret by construction: the setting store refuses credentials.
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @classmethod
    def of(
        cls,
        domains: list[Domain],
        records: list[DnsRecord],
        rules: list[Rule],
        *,
        settings: Mapping[str, JsonValue] | None = None,
    ) -> Self:
        """The state from the stores, put into canonical order.

        Sorted here rather than trusted, so equal state always yields equal bytes
        whatever order a restore or another engine returns. Rules sort by ``order``,
        the key ``resolve_policy`` uses.
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
            settings=dict(sorted((settings or {}).items())),
        )

    def encode(self) -> str:
        """The canonical bytes of this state: sorted keys, deterministic order."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True)

    @property
    def digest(self) -> str:
        """A stable identity for this state; a rollback stores it to detect changes."""
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
