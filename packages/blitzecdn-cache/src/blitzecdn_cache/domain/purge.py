"""One purge, and what the fleet did with it.

Split from the statistics beside it because they are two answers to two
questions: a purge is an instruction and its outcome per edge, while
`statistics` is a reading taken from logs that were written whether or not
anyone purged anything.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.validation import hostname

__all__ = ["PurgeEntry", "PurgeResult"]


class PurgeEntry(BaseModel):
    """One cached response to remove, named the way a client would request it.

    Deliberately a hostname and a path rather than a site name: the cache is
    keyed by the ``Host`` header nginx saw, and a site can answer to several
    hostnames. Purging "the site" would have to purge every one of them and
    still could not express "only the apex".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    uri: str
    #: The scheme a *client* used, not the one the edge fetches with. It leads
    #: the cache key, so it selects between two genuinely different entries;
    #: ``EdgeOperationsService`` refuses one the site could not have stored.
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return hostname(value)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        """Accept the request target nginx would have logged, and nothing else.

        No path normalization beyond stripping: cache keys preserve the raw
        path, so ``/a/./b`` and ``/a/b`` are genuinely different entries.
        ``CacheService`` removes only the query when the owning site explicitly
        uses ignore-query mode.
        """
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

    ``succeeded`` counts hosts that ran the removal, not entries deleted: nginx
    open source cannot report whether a key was present, so "deleted 3 files"
    and "there was nothing to delete" are the same observation. Treat a
    successful purge as "this object is not being served from cache any more",
    which is the question actually being asked, rather than as proof it was
    there.
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

    #: Serialised, not just computed. A partial purge is reported with the
    #: whole result rather than as a bare error string, so a client scripting a
    #: retry can read which edges still serve the object instead of parsing
    #: prose out of a ``detail`` field. That makes ``complete`` the flag the
    #: client branches on, which means it has to be in the body.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def complete(self) -> bool:
        """True only if every edge in scope purged.

        A partial purge is the dangerous outcome: the object is gone from some
        edges and still served by others, so which response a client gets
        depends on which edge answers.
        """
        return bool(self.hosts) and not self.failed

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_hosts(self) -> tuple[str, ...]:
        """The edges that may still be serving the cached copy, by name.

        Derivable from ``hosts``, but named here because it is the one field an
        operator acts on and it should not require walking a per-host result to
        find.
        """
        return tuple(host.host for host in self.failed)
