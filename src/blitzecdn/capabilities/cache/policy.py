"""Site cache policy, distinct from cache purge and statistics operations."""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, field_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import DURATION


class CacheQueryStringMode(StrEnum):
    """Whether request query strings distinguish cached responses."""

    INCLUDE = "include"
    IGNORE = "ignore"


class CachePolicy(CapabilityPolicy):
    """Cache behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_enabled: bool = True
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

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Implementation capabilities requested by this stable policy.

        A site opts *out* of caching, so the default asks for the detachable
        distribution. That is deliberate: an edge that silently stopped caching
        because a wheel was missing is the failure worth being loud about.
        """
        if not self.cache_enabled:
            return {}
        return {"cache": ("cache_enabled",)}
