"""The origin check's HTTP request. Its report is the queued operation's result."""

from __future__ import annotations

from blitzecdn.entrypoints.api.requests import FleetRequest


class OriginCheckRequest(FleetRequest):
    """Which edges should answer. All of them, unless narrowed."""


__all__ = ["OriginCheckRequest"]
