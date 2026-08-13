"""Request, response, and authentication state used by the HTTP adapter."""

from __future__ import annotations

import threading
import time
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.domain.cache import PurgeEntry
from blitzecdn.domain.certificates import CERTIFICATE_RENEWAL_DAYS
from blitzecdn.domain.runs import HostRun
from blitzecdn.domain.validation import EDGE_LIMIT


class AuthThrottle:
    """Cap failed authentications per client so API keys cannot be brute forced.

    Behind a reverse proxy every request appears to come from the proxy, which
    makes this a coarse global backstop rather than a per-client budget; put
    real per-client limiting in the proxy.
    """

    def __init__(self, *, limit: int = 10, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def allows(self, client: str) -> bool:
        with self._lock:
            return len(self._prune(client, time.monotonic())) < self._limit

    def record_failure(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(client, now)
            self._failures.setdefault(client, deque()).append(now)

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, client: str, now: float) -> deque[float]:
        failures = self._failures.get(client)
        if failures is None:
            return deque()
        cutoff = now - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        if not failures:
            del self._failures[client]
        return failures


class DeployRequest(BaseModel):
    check: bool = False
    #: Narrow the run to some edges — a canary. Validated by the same pattern
    #: the CLI uses, so a rejected limit is a 422 rather than a queued
    #: deployment that fails once it reaches Ansible.
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class DriftRequest(BaseModel):
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: Empty the cache instead of removing named entries. Kept as its own flag
    #: rather than "no entries means everything" so a caller whose filter
    #: matched nothing cannot empty the fleet's cache by accident.
    purge_all: bool = False
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class StatsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class OriginCheckRequest(BaseModel):
    """Which edges should answer. All of them, unless narrowed."""

    model_config = ConfigDict(extra="forbid")

    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class RenewRequest(BaseModel):
    within_days: int = Field(default=CERTIFICATE_RENEWAL_DAYS, ge=0, le=3650)
    force: bool = False
    #: Narrow the run to these sites. None means every managed certificate;
    #: an unknown name is a 404 rather than a quiet no-op.
    sites: list[str] | None = Field(default=None, min_length=1)


class RollbackRequest(BaseModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False


class EdgeRemoval(BaseModel):
    """Report the remote and local halves of deleting an edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: False when `?decommission=false` skipped the teardown entirely, leaving
    #: BlitzeCDN's configuration and TLS private keys on the host.
    decommissioned: bool
    hosts: tuple[HostRun, ...] = ()
