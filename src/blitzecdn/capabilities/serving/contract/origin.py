"""Origin request identity policy for a site.

Requests no capability: every managed edge sets a ``Host`` header and an SNI on
the origin leg whether or not anything is installed beside the control plane,
so this contract is answered by the default on :class:`CapabilityPolicy`.
"""

from pydantic import ConfigDict, field_validator

from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import hostname


class OriginPolicy(CapabilityPolicy):
    """Host header and SNI overrides used on the origin leg."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_request_host: str | None = None
    origin_sni: str | None = None

    @field_validator("origin_request_host", "origin_sni")
    @classmethod
    def validate_optional_host(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None
