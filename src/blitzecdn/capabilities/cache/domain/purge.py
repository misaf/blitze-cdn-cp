"""One purge, and what the fleet did with it."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    computed_field,
    field_validator,
)

from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.validation import hostname

__all__ = [
    "MAX_TAG",
    "PurgeEntry",
    "PurgePrefix",
    "PurgeResult",
    "PurgeTag",
    "purge_tag",
]

#: The longest request target nginx logs that a purge will name.
MAX_URI = 2048
#: The longest tag a purge will name, as Cloudflare caps one Cache-Tag value.
MAX_TAG = 1024


def purge_tag(value: str) -> str:
    """One tag as the origin names it in ``Cache-Tag``, folded to lowercase.

    Tags are case-insensitive: the edge lowercases what it reads from the
    stored header too, so ``Product-42`` purges entries tagged ``product-42``.
    A tag is one comma-separated item of the header's value, so it cannot
    contain a comma, and the edge trims whitespace around it, so it cannot
    contain any.
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("a tag cannot be empty")
    if len(candidate) > MAX_TAG:
        raise ValueError(f"a tag must be at most {MAX_TAG} characters")
    if "," in candidate or any(c.isspace() for c in candidate):
        raise ValueError("a tag cannot contain commas or whitespace")
    if not candidate.isascii() or not candidate.isprintable():
        raise ValueError("a tag must be printable ASCII")
    return candidate.lower()


#: Every cached response whose stored ``Cache-Tag`` header names this tag.
PurgeTag = Annotated[str, AfterValidator(purge_tag)]


def _request_target(value: str, field: str) -> str:
    candidate = value.strip()
    if not candidate.startswith("/"):
        raise ValueError(f"{field} must be an absolute path beginning with '/'")
    if len(candidate) > MAX_URI:
        raise ValueError(f"{field} must be at most {MAX_URI} characters")
    if any(character.isspace() for character in candidate):
        raise ValueError(f"{field} cannot contain whitespace")
    return candidate


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
        return _request_target(value, "uri")


class PurgePrefix(BaseModel):
    """Every cached response for a hostname under one request-target ``prefix``.

    The default prefix, ``/``, is the whole hostname. Both schemes, every
    listener port, method and encoding match: a prefix names content, and each
    of those variants serves it. Matching is by plain string prefix on the
    request target as nginx keyed it, so ``/blog`` also matches ``/blogroll``;
    end the prefix with ``/`` to name a directory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    prefix: str = "/"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return hostname(value)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return _request_target(value, "prefix")


class PurgeResult(BaseModel):
    """Which edges carried out a purge, and which did not.

    ``succeeded`` counts hosts that ran the removal: nginx cannot say whether a key
    existed, so success means "not served from cache now", not "it was there".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    prefixes: tuple[PurgePrefix, ...] = ()
    tags: tuple[PurgeTag, ...] = ()
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
