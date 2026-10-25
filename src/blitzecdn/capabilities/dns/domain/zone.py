"""Delegated zones and their default serving policy.

Records are stored separately, keyed by zone, and each carries its own origin.
Rules in ``dns.domain.rule`` override the policy per hostname.
"""

from __future__ import annotations

import ipaddress
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.contract import CertificateMode
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

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        """Keep certificate mode and both paths consistent; clear all three together."""
        supplied = (
            self.policy.tls.certificate_path is not None
            or self.policy.tls.certificate_key_path is not None
        )
        if self.policy.tls.certificate_mode is not CertificateMode.DISABLED and not (
            self.policy.tls.certificate_path and self.policy.tls.certificate_key_path
        ):
            raise ValueError("TLS certificate modes require both certificate paths")
        if self.policy.tls.certificate_mode is CertificateMode.DISABLED and supplied:
            raise ValueError("certificate paths require certificate_mode='existing'")
        # Whether a managed path is the right one is `CdnSite`'s check: a zone yields
        # several hosts and cannot tell which this is.
        if (
            self.policy.tls.ssl_mode.serves_tls
            and self.policy.tls.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.policy.tls.ssl_mode.value!r} "
                "requires an active edge certificate"
            )
        return self
