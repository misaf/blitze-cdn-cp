"""Effective virtual hosts, derived from a zone, its rules, and its records."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.http.contract import DEFAULT_PORTS, HttpScheme
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.contract import (
    MANAGED_TLS_ROOT,
    CertificateMode,
    CertificateVersion,
)
from blitzecdn.core.domain.validation import HOST_NAME, hostname


class CdnSite(BaseModel):
    """Validated desired state for one CDN virtual host.

    Its ``name`` already encodes the zone and rule it came from, via
    :func:`~blitzecdn.capabilities.dns.domain.hosts.host_name`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    policy: SitePolicy = Field(default_factory=SitePolicy)
    #: The hostnames this host answers on. Empty is legal and renders no server
    #: block.
    server_names: tuple[str, ...] = Field(default=(), max_length=100)
    origin_host: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HOST_NAME.fullmatch(normalized):
            raise ValueError(
                "name must contain only a-z, 0-9, and hyphens, and start with "
                "a letter or a digit"
            )
        return normalized

    @field_validator("server_names")
    @classmethod
    def validate_server_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(hostname(item, wildcard=True) for item in values)
        )
        if len(normalized) != len(values):
            raise ValueError("server_names must be unique")
        return normalized

    @field_validator("origin_host")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        return hostname(value)

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
        if self.policy.tls.certificate_mode.issuer_owned:
            try:
                CertificateVersion.from_paths(
                    self.name,
                    self.policy.tls.certificate_path or "",
                    self.policy.tls.certificate_key_path or "",
                )
            except ValueError as error:
                raise ValueError(
                    f"certificate_mode={self.policy.tls.certificate_mode.value!r} "
                    "is set by the "
                    "certificate upload and request endpoints, which own the paths "
                    "for exact versions "
                    f"under {MANAGED_TLS_ROOT}/<site>/"
                ) from error
        if (
            self.policy.tls.ssl_mode.serves_tls
            and self.policy.tls.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.policy.tls.ssl_mode.value!r} "
                "requires an active edge certificate"
            )
        return self

    @property
    def serves_traffic(self) -> bool:
        """Whether this site should contribute a server block at all.

        With no hostnames, an empty ``server_name`` would make it the listener's
        default server. ``enabled`` stays a separate switch.
        """
        return bool(self.server_names)

    @property
    def effective_origin_sni(self) -> str:
        """The name sent as SNI when the origin is reached over HTTPS.

        Never a wildcard: a literal ``*.example.com`` matches no certificate. Falls back
        to ``origin_host``; ``origin_sni`` overrides. ``site.conf.j2`` computes
        ``proxy_ssl_name`` identically, or preflight would verify the wrong name.
        """
        return (
            self.policy.origin.origin_sni
            or self.policy.origin.origin_request_host
            or self.origin_host
        )

    @property
    def serves_tls(self) -> bool:
        """Whether this site answers on 443."""
        return self.policy.tls.ssl_mode.serves_tls

    @property
    def canonical_visitor_scheme(self) -> HttpScheme:
        """The strongest scheme a visitor can reach this site on."""
        return HttpScheme.HTTPS if self.serves_tls else HttpScheme.HTTP

    @property
    def canonical_origin_scheme(self) -> HttpScheme:
        """The origin scheme for a canonical visitor, which is what preflight probes.

        See :class:`blitzecdn.capabilities.edges.adapters.probe.OriginProbe` for why
        only this endpoint, and what Flexible means for alternate ports.
        """
        scheme = self.canonical_visitor_scheme
        return self.policy.tls.ssl_mode.origin_scheme_for(scheme, DEFAULT_PORTS[scheme])

    @property
    def redirects_http_to_https(self) -> bool:
        """Whether the HTTP listeners redirect instead of proxying.

        ``always_use_https`` is inert while ``ssl_mode`` is off, as with Cloudflare:
        there is no HTTPS listener to send anyone to, and the preference applies again
        once TLS is on. ``site.conf.j2`` gates on the same condition, under test.
        """
        return self.serves_tls and self.policy.tls.always_use_https
