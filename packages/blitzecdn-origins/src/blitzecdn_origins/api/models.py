"""The HTTP representations this capability publishes, and the body it takes.

They live here rather than in ``blitzecdn.api.models`` for the reason the
whole extraction exists: a detachable package's resource shapes are the
package's, and core cannot carry an ``OriginReport`` for a capability that may
not be installed. What core still owns is the frame — ``Model``, the
``as_operation`` projection and ``HostRun`` — which every capability's
operational representation is built from, this one included.

Identical in v1 and v2, and defined once for the same reason the operational
models in core are: every published version has always accepted and returned
the same shape, and a second class would rename the other version's schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from blitzecdn.api.models import Model
from blitzecdn.api.requests import FleetRequest
from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.capabilities.tls.policy import SslMode


class OriginCheck(Model):
    site: str
    origin: str
    scheme: HttpScheme
    ssl_mode: SslMode
    sni: str | None = None
    reachable: bool = False
    tls_verified: bool | None = None
    status: int | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str | None = None


class EdgeOriginChecks(Model):
    host: str
    checked_at: datetime | None = None
    checks: tuple[OriginCheck, ...] = ()
    error: str | None = None


class OriginReport(Model):
    checked_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeOriginChecks, ...] = ()


class OriginCheckRequest(FleetRequest):
    """Which edges should answer. All of them, unless narrowed."""


__all__ = [
    "EdgeOriginChecks",
    "OriginCheck",
    "OriginCheckRequest",
    "OriginReport",
]
