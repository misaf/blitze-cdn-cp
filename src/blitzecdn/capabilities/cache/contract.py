"""Site cache policy, distinct from cache purge and statistics operations."""

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import ConfigDict, Field, field_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.values import Duration, NginxValue

#: A cookie name, optionally with ``*`` standing for any run of name characters.
#: Narrower than RFC 6265's token: the name reaches an nginx regex, and real
#: session cookies stay within letters, digits, ``_``, ``.`` and ``-``.
COOKIE_PATTERN = re.compile(r"^[A-Za-z0-9_.*-]{1,128}$")

#: What a TTL override or an ignore-origin entry names: a URL extension
#: (``.css``), a media type (``text/html``) or every subtype of one
#: (``image/*``). Lower case once normalized; like the cookie names, these reach
#: an nginx regex, so the alphabet is narrower than RFC 6838's.
CONTENT_PATTERN = re.compile(
    r"^(?:\.[a-z0-9]{1,16}|[a-z0-9][a-z0-9.+-]{0,63}/(?:\*|[a-z0-9][a-z0-9.+-]{0,63}))$"
)

#: An edge TTL: whole seconds, since it reaches nginx as ``X-Accel-Expires``.
#: ``0`` stores nothing of that type.
EDGE_TTL = re.compile(r"^(?:0|(?:0|[1-9]\d{0,8})[smhdw])$")


def _content_key(value: str) -> str:
    candidate = value.strip().lower()
    if not CONTENT_PATTERN.fullmatch(candidate):
        raise ValueError(
            f"{value!r} is neither a URL extension such as '.css' nor a "
            "content type such as 'text/html' or 'image/*'"
        )
    return candidate


def _cookie_pattern(value: str) -> str:
    candidate = value.strip()
    if not COOKIE_PATTERN.fullmatch(candidate):
        raise ValueError(
            f"{value!r} is not a cookie name: use letters, digits, "
            "'_', '.', '-' and '*' as a wildcard"
        )
    return candidate


def _edge_ttl(value: str) -> str:
    candidate = value.strip()
    if not EDGE_TTL.fullmatch(candidate):
        raise ValueError(
            f"{value!r} is not an edge TTL: use 0, or a whole number followed "
            "by s, m, h, d or w"
        )
    return candidate


#: A cookie name, ``*`` matching any run of name characters.
CookiePattern = Annotated[str, NginxValue("cookie name", _cookie_pattern)]

#: A URL extension, a media type or a media type family.
ContentKey = Annotated[str, NginxValue("content key", _content_key)]

#: An edge TTL in whole seconds or larger units.
EdgeTtl = Annotated[str, NginxValue("edge TTL", _edge_ttl)]


class CacheQueryStringMode(StrEnum):
    """Whether request query strings distinguish cached responses."""

    INCLUDE = "include"
    IGNORE = "ignore"


class CacheLevel(StrEnum):
    """What the edge stores when the origin says nothing about caching."""

    #: Only static file types, unless the origin sends caching headers; a
    #: request with a cookie skips the cache for anything else.
    STANDARD = "standard"
    #: Every 200, 301, 302 and 404, whatever its type. Opt-in: an origin that
    #: renders per-visitor pages without ``Cache-Control: private`` leaks them.
    EVERYTHING = "everything"


class CachePolicy(CapabilityPolicy):
    """Cache behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Rendered inside the location that proxies, with the cookie map declared
    # per path scope, so each can differ by path. Not `cache_query_string_mode`:
    # a purge rebuilds the cache key from the host's policy, so a key that
    # differed by path would let a purge report success and miss the entry.
    # Not the TTL overrides or ignored origin headers either: they rewrite the
    # response in the host's origin hop, which has no path scope.
    path_settings: ClassVar[frozenset[str]] = frozenset(
        {
            "cache_enabled",
            "cache_level",
            "cache_bypass_cookies",
            "cache_valid_success",
            "cache_valid_not_found",
        }
    )

    cache_enabled: bool = True
    cache_level: CacheLevel = Field(
        default=CacheLevel.STANDARD,
        description=(
            "'standard' stores only static file types (by URL extension or "
            "response Content-Type) unless the origin sends Cache-Control, "
            "Expires or X-Accel-Expires, and skips the cache for any other "
            "request carrying a bypass cookie. 'everything' stores every "
            "200, 301, 302 and 404 whatever its type."
        ),
    )
    cache_bypass_cookies: tuple[CookiePattern, ...] = Field(
        default=(),
        max_length=50,
        description=(
            "Cookie names whose presence skips the cache: neither served from "
            "nor stored in it. '*' matches any run of name characters, so "
            "'wordpress_logged_in_*' covers the suffixed variants. Empty means "
            "any cookie under 'standard' and none under 'everything'. Under "
            "'standard' static file URLs are cached whatever the cookies."
        ),
    )
    cache_ttl_overrides: dict[ContentKey, EdgeTtl] = Field(
        default_factory=dict,
        max_length=50,
        description=(
            "Edge TTL by URL extension ('.css'), content type ('text/html') "
            "or type family ('image/*'), e.g. {'image/*': '7d', "
            "'text/html': '0'}. Applies to 200, 301, 302 and 304 responses "
            "whose origin sent no Cache-Control, Expires or X-Accel-Expires, "
            "or to any of a type listed in cache_ignore_origin_headers. '0' "
            "stores nothing of that type. An extension wins over a type, and "
            "an exact type over a family. Units: s, m, h, d, w."
        ),
    )
    cache_ignore_origin_headers: tuple[ContentKey, ...] = Field(
        default=(),
        max_length=50,
        description=(
            "URL extensions, content types or type families whose origin "
            "Cache-Control, Expires and X-Accel-Expires the edge disregards "
            "when deciding how long to store them, for an origin that marks "
            "static assets no-cache by mistake. They are stored for their "
            "cache_ttl_overrides entry, else cache_valid_success (404: "
            "cache_valid_not_found; other statuses: not at all). Set-Cookie "
            "still keeps a response out. Visitors receive the origin's "
            "headers unchanged. Empty, the default, respects the origin."
        ),
    )
    cache_query_string_mode: CacheQueryStringMode = CacheQueryStringMode.INCLUDE
    cache_valid_success: Duration = "10m"
    cache_valid_not_found: Duration = "1m"

    @field_validator("cache_bypass_cookies", "cache_ignore_origin_headers")
    @classmethod
    def deduplicate(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    @field_validator("cache_ttl_overrides", mode="before")
    @classmethod
    def refuse_repeated_keys(cls, values: object) -> object:
        """Two spellings of one key would otherwise collapse silently."""
        if not isinstance(values, Mapping):
            return values
        seen: set[str] = set()
        for key in values:
            candidate = str(key).strip().lower()
            if candidate in seen:
                raise ValueError(f"{key!r} is listed twice")
            seen.add(candidate)
        return values

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """The ``cache`` capability, while caching (the default) is enabled."""
        if not self.cache_enabled:
            return {}
        return {"cache": ("cache_enabled",)}
