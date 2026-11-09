"""Site cache policy, distinct from cache purge and statistics operations."""

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import DURATION

#: A cookie name, optionally with ``*`` standing for any run of name characters.
#: Narrower than RFC 6265's token: the name reaches an nginx regex, and real
#: session cookies stay within letters, digits, ``_``, ``.`` and ``-``.
COOKIE_PATTERN = re.compile(r"^[A-Za-z0-9_.*-]{1,128}$")


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
    cache_bypass_cookies: tuple[str, ...] = Field(
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
    cache_query_string_mode: CacheQueryStringMode = CacheQueryStringMode.INCLUDE
    cache_valid_success: str = "10m"
    cache_valid_not_found: str = "1m"

    @field_validator("cache_valid_success", "cache_valid_not_found")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if not DURATION.fullmatch(value):
            raise ValueError(
                "duration must be a non-negative integer followed by "
                "ms, s, m, h, d, or w"
            )
        return value

    @field_validator("cache_bypass_cookies")
    @classmethod
    def validate_cookies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.strip()
            if not COOKIE_PATTERN.fullmatch(candidate):
                raise ValueError(
                    f"{value!r} is not a cookie name: use letters, digits, "
                    "'_', '.', '-' and '*' as a wildcard"
                )
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(normalized)

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """The ``cache`` capability, while caching (the default) is enabled."""
        if not self.cache_enabled:
            return {}
        return {"cache": ("cache_enabled",)}
