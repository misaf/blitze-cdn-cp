"""The TLS capability's configuration contract: modes, versions and managed paths.

Pure values; issuance and the Automatic SSL/TLS scan live in
``tls.certificates`` and ``tls.automatic_ssl``.
"""

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.http.contract import (
    DEFAULT_PORTS,
    HttpScheme,
    ProtocolPolicy,
)
from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.features.types import PolicyView

MANAGED_TLS_ROOT = "/etc/blitzecdn/tls"
CERTIFICATE_ROOTS = (f"{MANAGED_TLS_ROOT}/", "/etc/ssl/", "/etc/letsencrypt/")

#: Every character a certificate path may contain. ``site.conf.j2`` writes it
#: unquoted into ``ssl_certificate``, so a semicolon or newline would end the
#: directive and start one of the author's choosing on every edge.
CERTIFICATE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


class CertificateVersion(BaseModel):
    """The exact immutable bytes selected by canonical TLS policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    certificate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_paths(
        cls, site_name: str, certificate: str, key: str
    ) -> "CertificateVersion":
        prefix = f"{MANAGED_TLS_ROOT}/{site_name}/"
        chain = re.fullmatch(
            re.escape(prefix) + r"fullchain-([0-9a-f]{64})\.pem", certificate
        )
        private = re.fullmatch(re.escape(prefix) + r"privkey-([0-9a-f]{64})\.pem", key)
        if chain is None or private is None:
            raise ValueError(
                "managed certificate paths must name exact versions under their site"
            )
        return cls(certificate_digest=chain[1], private_key_digest=private[1])


def managed_certificate_paths(
    site_name: str, version: CertificateVersion
) -> tuple[str, str]:
    """Return the content-addressed edge paths for a selected version."""
    return (
        f"{MANAGED_TLS_ROOT}/{site_name}/fullchain-{version.certificate_digest}.pem",
        f"{MANAGED_TLS_ROOT}/{site_name}/privkey-{version.private_key_digest}.pem",
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
        """The origin scheme for one visitor request.

        Full modes mirror visitor transport; Flexible uses HTTP behind HTTPS/443 and
        HTTPS behind alternate HTTPS proxy ports.
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

    def origin_port_for(self, visitor_scheme: HttpScheme, visitor_port: int) -> int:
        """The origin port for one visitor request: the visitor's own, except
        Flexible HTTPS/443, which reaches the HTTP origin on 80."""
        https = DEFAULT_PORTS[HttpScheme.HTTPS]
        if (
            self is SslMode.FLEXIBLE
            and visitor_scheme is HttpScheme.HTTPS
            and visitor_port == https
        ):
            return DEFAULT_PORTS[HttpScheme.HTTP]
        return visitor_port

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

    @property
    def issuer_owned(self) -> bool:
        """Whether the certificates capability, not an operator, sets this mode.

        ``uploaded`` and ``requested`` use paths derived from the host's name, which
        only the issuer knows; ``disabled`` and ``existing`` are the operator's.
        """
        return self in {CertificateMode.UPLOADED, CertificateMode.REQUESTED}


class TlsPolicy(CapabilityPolicy):
    """TLS settings contained in the composed serving policy."""

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

        A deploy copies these paths as root, hence the traversal and root checks; the
        value is rendered unquoted into nginx, hence the character check.
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

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        """Keep certificate mode and both paths consistent; clear all three together.

        Whether a managed path is the right one is
        :func:`managed_certificate_belongs_to_site`: a zone yields several hosts
        and cannot tell which this is.
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
        if (
            self.ssl_mode.serves_tls
            and self.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.ssl_mode.value!r} requires an active edge certificate"
            )
        return self

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """The certificate capabilities this policy requests.

        Managed material and Automatic SSL need ``certificates``; existing material
        needs nothing. Managed material is named first, as the setting to clear.
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


def http3_requires_edge_tls(policy: PolicyView) -> None:
    """HTTP/3 is QUIC, and QUIC carries TLS: there is no plaintext HTTP/3."""
    if (
        policy[ProtocolPolicy].http3_enabled
        and not policy[TlsPolicy].ssl_mode.serves_tls
    ):
        raise ValueError("http3_enabled=True requires ssl_mode to serve edge TLS")


def managed_certificate_belongs_to_site(site: str, policy: PolicyView) -> None:
    """Managed material lives at exact version paths under the host's own name."""
    tls = policy[TlsPolicy]
    if not tls.certificate_mode.issuer_owned:
        return
    try:
        CertificateVersion.from_paths(
            site, tls.certificate_path or "", tls.certificate_key_path or ""
        )
    except ValueError as error:
        raise ValueError(
            f"certificate_mode={tls.certificate_mode.value!r} is set by the "
            "certificate upload and request endpoints, which own the paths "
            f"for exact versions under {MANAGED_TLS_ROOT}/<site>/"
        ) from error
