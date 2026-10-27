"""The HTTP contract for accepting and polling background work."""

from typing import Any

from fastapi import Response

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
