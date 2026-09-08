"""Delegated zones and their default serving policy.

Records are stored separately and keyed by zone. Hostname-specific overrides
live in ``dns.domain.rule``; ``dns.domain.resolution`` merges them with the
zone policy.

``origin_host`` may be omitted while configuring a zone. Deployment validation
requires an effective origin for each proxied hostname.

``SitePolicy`` is shared with derived virtual hosts through ``dns.domain.host``.
``_assert_patch_covers_zone`` checks that partial updates cover the zone fields."""

from __future__ import annotations

import ipaddress
from typing import Self

from pydantic import ConfigDict, field_validator, model_validator

from blitzecdn.capabilities.dns.domain.host import SitePolicy
from blitzecdn.capabilities.tls.policy import CERTIFICATE_ROOTS, CertificateMode
from blitzecdn.core.domain.validation import hostname

__all__ = ["Domain"]


class Domain(SitePolicy):
    """A DNS zone a customer has delegated to us, and its default policy.

    Holds no records itself — they are stored separately and keyed by domain,
    so a zone with a thousand records is not rewritten to change one of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: Where the edge fetches from for hostnames in this zone that are proxied
    #: and have no rule saying otherwise. Optional because a zone is delegable
    #: long before anything is served from it; see the module docstring.
    origin_host: str | None = None

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

    @field_validator("origin_host")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        return None if value is None else hostname(value)

    @field_validator("certificate_path", "certificate_key_path")
    @classmethod
    def validate_remote_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError(
                "certificate paths must be absolute and cannot contain '..'"
            )
        if not value.startswith(CERTIFICATE_ROOTS):
            raise ValueError(
                "certificate paths must live under one of: "
                + ", ".join(CERTIFICATE_ROOTS)
            )
        return value

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        """Keep certificate mode and the two paths mutually consistent.

        The same rule the site carried, asked of the zone that carries the
        policy now. Turning TLS off means clearing ``certificate_mode``,
        ``certificate_path`` and ``certificate_key_path`` together in a single
        update; a patch that only sets the mode to ``disabled`` is rejected.
        """
        supplied = (
            self.certificate_path is not None or self.certificate_key_path is not None
        )
        if self.certificate_mode is not CertificateMode.DISABLED and not (
            self.certificate_path and self.certificate_key_path
        ):
            raise ValueError("TLS certificate modes require both certificate paths")
        if self.certificate_mode is CertificateMode.DISABLED and supplied:
            raise ValueError("certificate paths require certificate_mode='existing'")
        # Whether a managed path is the *right* managed path is not asked
        # here. These paths name the virtual host a certificate was issued for,
        # and a zone produces several — its own and one per rule — so the zone
        # cannot say which of them this is. `CdnSite` can, because its name is
        # exactly that, and it asks the same question there.
        if (
            self.ssl_mode.serves_tls
            and self.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.ssl_mode.value!r} requires an active edge certificate"
            )
        return self
