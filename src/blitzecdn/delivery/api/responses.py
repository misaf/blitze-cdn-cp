"""The HTTP contract for created resources and for accepting and polling work."""

from typing import Any
from urllib.parse import quote

from fastapi import Response

from blitzecdn.delivery.api.preconditions import ETAG_RESPONSE

POLL_INTERVAL_SECONDS = 2

_POLL_HEADERS: dict[str, Any] = {
    "Retry-After": {
        "description": "Suggested seconds before polling again; absent when finished.",
        "schema": {"type": "integer", "minimum": 1},
        "example": POLL_INTERVAL_SECONDS,
    },
    "Cache-Control": {
        "description": "Do not cache operation status.",
        "schema": {"type": "string", "enum": ["no-store"]},
    },
}

ACCEPTED_RESPONSE: dict[str, Any] = {
    "description": "Work accepted, not completed. Poll the URL in Location.",
    "headers": {
        **_POLL_HEADERS,
        "Location": {
            "description": "Relative URL of the authenticated status resource.",
            "schema": {"type": "string"},
        },
    },
}

CREATED_RESPONSE: dict[str, Any] = {
    "description": "Created. Location is the new resource's canonical URL.",
    "headers": {
        "Location": {
            "description": "Relative URL of the created resource.",
            "schema": {"type": "string"},
        },
    },
}

#: A 201 for a versioned resource: its Location and the ETag of what was stored.
TAGGED_CREATED_RESPONSE: dict[str, Any] = {
    **CREATED_RESPONSE,
    "headers": {**CREATED_RESPONSE["headers"], **ETAG_RESPONSE["headers"]},
}

POLL_RESPONSE: dict[str, Any] = {
    "description": "Current status; HTTP 200 does not imply the work succeeded.",
    "headers": _POLL_HEADERS,
}


def polling_headers(
    response: Response, *, active: bool, location: str | None = None
) -> None:
    if location is not None:
        response.headers["Location"] = location
    if active:
        response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
    response.headers["Cache-Control"] = "no-store"


def created_at(response: Response, path: str, *segments: str) -> None:
    """Point ``Location`` at a created resource: ``path`` plus quoted ``segments``.

    Each segment is one path component, quoted whole, so a name holding ``/``
    or ``?`` cannot turn into a different URL.
    """
    location = "/".join([path, *(quote(segment, safe="") for segment in segments)])
    response.headers["Location"] = location
