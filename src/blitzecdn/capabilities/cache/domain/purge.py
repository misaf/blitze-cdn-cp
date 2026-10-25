"""One purge, and what the fleet did with it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.validation import hostname

__all__ = ["PurgeEntry", "PurgeResult"]


class PurgeEntry(BaseModel):
    """One cached response to remove, by hostname and path as a client requests it.

    The cache is keyed by ``Host``, and one site answers to several hostnames.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    uri: str
    #: The client's scheme, which leads the cache key.
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return hostname(value)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        """The request target as nginx logged it; ``/a/./b`` is its own entry."""
        candidate = value.strip()
        if not candidate.startswith("/"):
            raise ValueError("uri must be an absolute path beginning with '/'")
        if len(candidate) > 2048:
            raise ValueError("uri must be at most 2048 characters")
        if any(character.isspace() for character in candidate):
            raise ValueError("uri cannot contain whitespace")
        return candidate


class PurgeResult(BaseModel):
    """Which edges carried out a purge, and which did not.

    ``succeeded`` counts hosts that ran the removal: nginx cannot say whether a key
    existed, so success means "not served from cache now", not "it was there".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()

    @property
    def succeeded(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if host.succeeded)

    @property
    def failed(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.succeeded)

    #: Serialised, so a scripted retry can branch on it from the body.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def complete(self) -> bool:
        """True only if every edge purged; a partial purge serves both versions."""
        return bool(self.hosts) and not self.failed

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_hosts(self) -> tuple[str, ...]:
        """The edges that may still be serving the cached copy, by name."""
        return tuple(host.host for host in self.failed)
