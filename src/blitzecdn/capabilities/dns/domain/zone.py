"""Delegated zones and their default serving policy.

Records are stored separately, keyed by zone, and each carries its own origin.
Rules in ``dns.domain.rule`` override the policy per hostname.
"""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, ConfigDict, Field, field_validator

from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.core.domain.validation import hostname

__all__ = ["Domain"]


class Domain(BaseModel):
    """A DNS zone delegated to us, and its default policy; records are stored apart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    policy: SitePolicy = Field(default_factory=SitePolicy)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = hostname(value)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            if "." not in normalized:
                raise ValueError(
                    "domain must be a delegable zone such as 'example.com'"
                ) from None
            return normalized
        raise ValueError("domain must be a name, not an IP address")
