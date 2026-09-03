"""The TLS capability's configuration contract.

Pure values: the modes, versions and managed paths that describe how a site's
TLS is *configured*. The behaviour they name — issuing, uploading, renewing and
publishing material, and the Automatic SSL/TLS scan that upgrades ``ssl_mode``
— lives in the optional ``blitzecdn-certificates`` distribution.

This module imports nothing but ``core`` and another capability's policy
contract, which is what lets ``sites`` compose it without depending on the TLS
implementation that consumes ``CdnSite``.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict

from blitzecdn.capabilities.http.policy import DEFAULT_PORTS, HttpScheme
from blitzecdn.core.policy import CapabilityPolicy

MANAGED_TLS_ROOT = "/etc/blitzecdn/tls"
CERTIFICATE_ROOTS = (f"{MANAGED_TLS_ROOT}/", "/etc/ssl/", "/etc/letsencrypt/")


def managed_certificate_paths(site_name: str) -> tuple[str, str]:
    """Return the chain and key paths BlitzeCDN manages for ``site_name``."""
    return (
        f"{MANAGED_TLS_ROOT}/{site_name}/fullchain.pem",
        f"{MANAGED_TLS_ROOT}/{site_name}/privkey.pem",
    )


class SslMode(StrEnum):
    """How TLS is used on both sides of an edge connection."""

    OFF = "off"
    FLEXIBLE = "flexible"
    FULL = "full"
    FULL_STRICT = "full_strict"

    @property
    def serves_tls(self) -> bool:
        return self is not SslMode.OFF

    def origin_scheme_for(
        self, visitor_scheme: HttpScheme, visitor_port: int
    ) -> HttpScheme:
        """Return the origin scheme for one visitor request.

        Full modes mirror visitor transport. Flexible uses HTTP behind visitor
        HTTPS/443 and falls back to HTTPS on alternate HTTPS proxy ports.
        """
        if visitor_scheme is HttpScheme.HTTP:
            return HttpScheme.HTTP
        if self is SslMode.OFF:
            return HttpScheme.HTTP
        if self is SslMode.FLEXIBLE:
            return (
                HttpScheme.HTTP
                if visitor_port == DEFAULT_PORTS[HttpScheme.HTTPS]
                else HttpScheme.HTTPS
            )
        return HttpScheme.HTTPS

    @property
    def verifies_origin(self) -> bool:
        return self is SslMode.FULL_STRICT

    @property
    def security_rank(self) -> int:
        """Monotonic order used by Automatic SSL/TLS upgrades."""
        return {
            SslMode.OFF: 0,
            SslMode.FLEXIBLE: 1,
            SslMode.FULL: 2,
            SslMode.FULL_STRICT: 3,
        }[self]


class SslAutomaticMode(StrEnum):
    """Whether the control plane may upgrade the selected SSL mode."""

    AUTO = "auto"
    CUSTOM = "custom"


class MinimumTlsVersion(StrEnum):
    """Oldest TLS protocol a visitor may use at the edge."""

    TLS_1_2 = "1.2"
    TLS_1_3 = "1.3"


class CertificateMode(StrEnum):
    """How the site's edge certificate material is managed."""

    DISABLED = "disabled"
    EXISTING = "existing"
    UPLOADED = "uploaded"
    REQUESTED = "requested"


class TlsPolicy(CapabilityPolicy):
    """TLS settings persisted as part of a site's flat policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ssl_mode: SslMode = SslMode.OFF
    ssl_automatic_mode: SslAutomaticMode = SslAutomaticMode.AUTO
    minimum_tls_version: MinimumTlsVersion = MinimumTlsVersion.TLS_1_2
    always_use_https: bool = False
    certificate_mode: CertificateMode = CertificateMode.DISABLED
    certificate_path: str | None = None
    certificate_key_path: str | None = None

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Operational certificate capabilities requested by this TLS policy.

        Existing edge material is a core TLS contract. Controller-managed
        material, and Automatic SSL on an active certificate, require the
        detachable certificates implementation.

        Managed material is named ahead of the automatic scan because it is the
        more specific answer: a site that both uploads material and asks for
        the scan is refused over ``certificate_mode``, which is the setting an
        operator would clear to make the deployment legal.
        """
        if self.certificate_mode in {
            CertificateMode.UPLOADED,
            CertificateMode.REQUESTED,
        }:
            return {"certificates": ("certificate_mode",)}
        if (
            self.certificate_mode is not CertificateMode.DISABLED
            and self.ssl_automatic_mode is SslAutomaticMode.AUTO
        ):
            return {"certificates": ("ssl_automatic_mode",)}
        return {}
