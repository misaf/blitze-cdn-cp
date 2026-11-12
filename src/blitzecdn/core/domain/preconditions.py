"""Optimistic concurrency: an entity tag per stored resource, and ``If-Match``.

A store's compare-and-swap (``replace_*(updated, expected=current)``) keeps two
writers racing on different fields from losing either. It cannot tell that a
client built its edit from a read that is no longer current: a list sent whole,
or a value worked out from the previous one, overwrites whatever landed in
between. ``If-Match`` names the version the client read, and the service checks
it against the version it read inside its transaction, before writing anything.

The tag is a digest of the resource's canonical JSON, so it changes exactly when
the stored value does and needs no revision column. It is strong: two versions
that serialize the same are the same version.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel

from blitzecdn.core.domain.documents import canonical_document
from blitzecdn.core.exceptions import PreconditionFailedError

__all__ = ["IfMatch", "entity_tag", "require_current"]


def entity_tag(value: BaseModel) -> str:
    """The strong entity tag of a stored resource, quoted as HTTP sends it."""
    content = canonical_document(value.model_dump(mode="json"))
    return f'"{hashlib.sha256(content.encode()).hexdigest()[:32]}"'


@dataclass(frozen=True, slots=True)
class IfMatch:
    """The versions a client says its edit was based on.

    ``*`` matches any current version. Otherwise the header is a list of quoted
    tags, compared strongly: a weak tag (``W/"..."``) never matches, as RFC 9110
    requires for ``If-Match``. An element that is not a tag matches nothing, so a
    mangled header fails safe with 412 rather than writing.
    """

    tags: frozenset[str]
    any_version: bool = False

    @classmethod
    def parse(cls, header: str) -> Self:
        value = header.strip()
        if value == "*":
            return cls(tags=frozenset(), any_version=True)
        return cls(
            tags=frozenset(part.strip() for part in value.split(",") if part.strip())
        )

    def matches(self, current: BaseModel) -> bool:
        return self.any_version or entity_tag(current) in self.tags

    def require(self, current: BaseModel, resource: str) -> None:
        """Raise ``PreconditionFailedError`` unless ``current`` is a version named."""
        if not self.matches(current):
            raise PreconditionFailedError(
                f"{resource} has changed since the version named in If-Match; "
                "read it again and reapply the edit"
            )


def require_current(
    precondition: IfMatch | None, current: BaseModel, resource: str
) -> None:
    """Check ``precondition`` when the client sent one; without it, write as before."""
    if precondition is not None:
        precondition.require(current, resource)
