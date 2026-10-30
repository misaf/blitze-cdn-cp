"""What TLS writes back onto a host's zone or rule, and when it may.

DNS owns where a host's policy comes from and writes it; which settings an
issued certificate or an Automatic SSL upgrade changes is TLS's decision.
"""

from __future__ import annotations

from blitzecdn.capabilities.dns.domain import SiteChange
from blitzecdn.capabilities.tls.certificates.ports import SiteWriter
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
    managed_certificate_paths,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import NotFoundError

__all__ = ["SiteEditing"]


class SiteEditing:
    """The TLS writes, each made through DNS in one transaction."""

    def __init__(self, *, writer: SiteWriter) -> None:
        self.writer = writer

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode, version: CertificateVersion
    ) -> CdnSite:
        """Record a managed certificate on the zone or rule that produced this host."""
        certificate_path, certificate_key_path = managed_certificate_paths(
            site.name, version
        )
        change = SiteChange(
            {
                "certificate_mode": mode,
                "certificate_path": certificate_path,
                "certificate_key_path": certificate_key_path,
            }
        )
        updated = self.writer.change_site(site.name, lambda _current: change)
        if updated is None:  # pragma: no cover - the decision above always writes
            raise NotFoundError(f"CDN site {site.name!r} does not exist")
        return updated

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None:
        """Persist an upgrade only while the host remains enrolled in Auto.

        The scan ran long before this, so both guards are judged on the host as
        stored when the write happens: an operator who opts out or picks a
        stronger mode meanwhile wins.
        """

        def decide(current: CdnSite) -> SiteChange | None:
            tls = current.policy[TlsPolicy]
            if tls.ssl_automatic_mode is SslAutomaticMode.CUSTOM:
                return None
            if target.security_rank <= tls.ssl_mode.security_rank:
                return None
            return SiteChange(
                {"ssl_mode": target},
                domain_event(
                    operator,
                    "ssl.automatic.upgraded",
                    "site",
                    site_name,
                    {"from": tls.ssl_mode.value, "to": target.value},
                ),
            )

        return self.writer.change_site(site_name, decide)
