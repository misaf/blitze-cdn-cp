"""The delegated zone, and how every hostname in it is served by default.

Its own module because it is its own aggregate. A zone holds no records — they
are stored separately and keyed by zone, so a zone with a thousand records is
not rewritten to change one of them — and the name's own invariant, that it is
delegable, is a different question from anything
:mod:`~blitzecdn.capabilities.dns.domain.record` asks.

What is new here is the policy. A zone used to be a name and nothing else,
with every setting living on a site that records pointed at. The settings are
here now: one delegated domain is one configuration, which is the shape an
operator arriving from Cloudflare already has in mind, and the hostnames that
need to differ from it say so through a rule rather than through a second
object each. :mod:`~blitzecdn.capabilities.rules` holds those overrides and
:mod:`~blitzecdn.capabilities.dns.service.resolution` merges the two.

``origin_host`` is optional here and required of anything actually served. A
zone is added before anyone has decided what it proxies to — Cloudflare does
not ask for an origin when a site is added either — so demanding one up front
would put a placeholder in the field that matters most. Resolution is where
its absence becomes an error, and only for a hostname that asked to be
proxied.

``SitePolicy`` is composed in
:mod:`~blitzecdn.capabilities.dns.domain.host` and inherited here rather than
declared twice. It is the same twenty settings whichever object carries them —
the zone that authors them and the virtual host they are resolved into — and
two lists of capability contracts to keep in step is exactly the failure
``_assert_patch_covers_zone`` exists to prevent.
"""

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
