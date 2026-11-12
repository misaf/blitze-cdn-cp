"""``ETag`` on single-resource reads and writes, ``If-Match`` on edits.

A client that builds an edit from what it read (a list sent whole, a value
derived from the old one) sends the ``ETag`` of that read as ``If-Match``. The
service compares it with the version it reads inside its write transaction, so
the check and the write cannot be separated by another writer. A stale tag
answers ``412`` and writes nothing. Without ``If-Match`` a write behaves as it
always has: the last write wins, field by field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Header, Response
from pydantic import BaseModel

from blitzecdn.core.domain.preconditions import IfMatch, entity_tag

__all__ = [
    "ETAG_RESPONSE",
    "IfMatchHeader",
    "if_match",
    "tag",
]

#: The ``If-Match`` request header an edit accepts.
IfMatchHeader = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description=(
            "The `ETag` of the version this edit was based on, or `*`. If the "
            "resource has changed since, the request answers 412 and writes "
            "nothing. Omit it to write whatever the current version is."
        ),
    ),
]

_ETAG_HEADER: dict[str, Any] = {
    "ETag": {
        "description": (
            "Strong tag of the version returned. Send it back as `If-Match` on "
            "a PATCH or DELETE built from this representation."
        ),
        "schema": {"type": "string"},
    }
}

#: A success response that carries the resource's ``ETag``.
ETAG_RESPONSE: dict[str, Any] = {"headers": _ETAG_HEADER}


def if_match(header: str | None) -> IfMatch | None:
    """The precondition a request named, or ``None`` when it named none."""
    return None if header is None else IfMatch.parse(header)


def tag[T: BaseModel](response: Response, resource: T) -> T:
    """Set ``ETag`` for the stored ``resource`` and hand it back."""
    response.headers["ETag"] = entity_tag(resource)
    return resource
