"""Delegated zones and their default serving policy.

Records are stored separately and keyed by zone. Hostname-specific overrides
live in ``dns.domain.rule``; ``dns.domain.resolution`` merges them with the
zone policy.

The zone holds no origin: each record carries its own address, which is the
origin the edge fetches from while the record is proxied. ``SitePolicy`` is
shared with derived virtual hosts through ``serving.domain.host``.
``_assert_patch_covers_settings`` checks partial-update coverage of composed
settings."""

from __future__ import annotations

import ipaddress
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.contract import CertificateMode
from blitzecdn.core.domain.validation import hostname

__all__ = ["Domain"]


class Domain(BaseModel):
    """A DNS zone a customer has delegated to us, and its default policy.

    Holds no records itself — they are stored separately and keyed by domain,
    so a zone with a thousand records is not rewritten to change one of them.
    """

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
        """Keep certificate mode and the two paths mutually consistent.

        The same rule the site carried, asked of the zone that carries the
        policy now. Turning TLS off means clearing ``certificate_mode``,
        ``certificate_path`` and ``certificate_key_path`` together in a single
        update; a patch that only sets the mode to ``disabled`` is rejected.
        """
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
        # Whether a managed path is the *right* managed path is not asked
        # here. These paths name the virtual host a certificate was issued for,
        # and a zone produces several — its own and one per rule — so the zone
        # cannot say which of them this is. `CdnSite` can, because its name is
        # exactly that, and it asks the same question there.
        if (
            self.policy.tls.ssl_mode.serves_tls
            and self.policy.tls.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.policy.tls.ssl_mode.value!r} "
                "requires an active edge certificate"
            )
        return self
