"""The TLS capability's configuration contract.

Pure values: the modes, versions and managed paths that describe how a site's
TLS is *configured*. The behaviour they name — issuing, uploading, renewing and
publishing material, and the Automatic SSL/TLS scan that upgrades ``ssl_mode``
— lives in the optional ``blitzecdn-certificates`` distribution.

This module imports nothing but ``core`` and another capability's policy
contract, which is what lets ``dns`` compose it without depending on the TLS
implementation that consumes ``CdnSite``.
"""

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, field_validator

from blitzecdn.capabilities.http.policy import DEFAULT_PORTS, HttpScheme
from blitzecdn.core.domain.policy import CapabilityPolicy

MANAGED_TLS_ROOT = "/etc/blitzecdn/tls"
CERTIFICATE_ROOTS = (f"{MANAGED_TLS_ROOT}/", "/etc/ssl/", "/etc/letsencrypt/")

#: Every character a certificate path may contain.
#:
#: Here rather than in ``core.domain.validation`` for the reason that module
#: states about itself: a shape with exactly one consumer belongs with the
#: contract that validates against it, and this one has exactly the consumer
#: below. ``CERTIFICATE_ROOTS`` already lives here for the same reason.
#:
#: The set is what an nginx directive argument may safely hold, not what a
#: filesystem will accept. ``ssl_certificate {{ path }};`` in the nginx role's
#: ``site.conf.j2`` interpolates the value unquoted, so a path containing a
#: semicolon or a newline does not name an unusual file — it ends the directive
#: and starts one of the author's choosing, on every edge the site converges to.
#: The three checks below therefore bound *where* the path points; this one
#: bounds what it is able to say once it is written into the config.
CERTIFICATE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


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
    """How the site's edge certificate material is managed.

    The four split in two along ``issuer_owned``: who is entitled to write the
    mode and the pair of paths that must agree with it.
    """

    DISABLED = "disabled"
    EXISTING = "existing"
    UPLOADED = "uploaded"
    REQUESTED = "requested"

    @property
    def issuer_owned(self) -> bool:
        """Whether the certificates capability, not an operator, sets this.

        ``uploaded`` and ``requested`` name material the control plane put on
        the edge itself, under a path derived from the *derived host's* name.
        Only the issuer knows that name — a zone produces several hosts, its
        own and one per rule — so only the issuer can write the mode and the
        two paths consistently. ``CdnSite`` refuses the pair when they
        disagree, and the operator-facing edits refuse the mode outright.

        ``disabled`` and ``existing`` are the operator's: the first says the
        host serves no TLS, the second points at material somebody else put on
        the box, which the control plane neither issues nor renews.
        """
        return self in {CertificateMode.UPLOADED, CertificateMode.REQUESTED}


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

    @field_validator("certificate_path", "certificate_key_path")
    @classmethod
    def validate_remote_path(cls, value: str | None) -> str | None:
        """Bound where an edge certificate path may point, and what it may say.

        On the contract that declares the two fields, so that every model
        carrying them inherits one rule. It was written out twice — on
        ``CdnSite`` and again on ``Domain`` — which is the arrangement
        ``dns.domain.host`` describes itself as not having: a rule about one
        capability's own fields belongs to that capability, and the composition
        keeps only what reads across two of them. Two copies of a security
        check are also two things that can be tightened separately, and the
        pair had to agree for either to mean anything.

        A deploy copies these paths as root, which is what the traversal and
        root checks are about. The character check is about the other end: the
        value is rendered unquoted into an nginx directive.
        """
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
        if not CERTIFICATE_PATH.fullmatch(value):
            raise ValueError(
                "certificate paths may contain only letters, digits, '.', '_', "
                "'-' and '/'"
            )
        return value

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
