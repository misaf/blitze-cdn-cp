"""The edge virtual host, and the document the edge roles consume.

``CdnSite`` is the whole of what the control plane asks an edge to serve. It is
derived rather than authored — :mod:`blitzecdn.domain.dns` owns the records it
comes from — so nothing here reads or writes state; it validates a shape and
renders it.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.domain.validation import (
    COUNTRY_ALIASES,
    COUNTRY_CODE,
    DURATION,
    HTTP_METHOD,
    ISO_3166_1_ALPHA_2,
    SITE_NAME,
    hostname,
    unique,
)

#: Certificates BlitzeCDN installs itself live here, one directory per site.
MANAGED_TLS_ROOT = "/etc/blitzecdn/tls"

#: Deploying a site makes Ansible write these paths on every edge as root, so an
#: operator must not be able to aim them at arbitrary files such as /etc/cron.d.
_CERTIFICATE_ROOTS = (f"{MANAGED_TLS_ROOT}/", "/etc/ssl/", "/etc/letsencrypt/")


def managed_certificate_paths(site_name: str) -> tuple[str, str]:
    """Return the chain and key paths BlitzeCDN manages for ``site_name``."""
    return (
        f"{MANAGED_TLS_ROOT}/{site_name}/fullchain.pem",
        f"{MANAGED_TLS_ROOT}/{site_name}/privkey.pem",
    )


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class SslMode(StrEnum):
    """How TLS is used on both sides of an edge connection."""

    OFF = "off"
    FLEXIBLE = "flexible"
    FULL = "full"
    FULL_STRICT = "full_strict"

    @property
    def serves_tls(self) -> bool:
        return self is not SslMode.OFF

    @property
    def origin_scheme(self) -> HttpScheme:
        if self in {SslMode.FULL, SslMode.FULL_STRICT}:
            return HttpScheme.HTTPS
        return HttpScheme.HTTP

    @property
    def verifies_origin(self) -> bool:
        return self is SslMode.FULL_STRICT


class CertificateMode(StrEnum):
    DISABLED = "disabled"
    EXISTING = "existing"
    UPLOADED = "uploaded"
    REQUESTED = "requested"


class SiteFirewall(BaseModel):
    """Per-hostname request filtering applied at the edge.

    This has to live in nginx rather than in ``blitzecdn_firewall``. A packet
    filter sees an address, a port and a protocol; it does not see the ``Host``
    header, and on an edge every managed site shares one address and one pair
    of ports. Filtering per hostname is only possible once the request has been
    parsed, which means the virtual host.

    **The posture is open.** An empty block behaves exactly as no block at all,
    and the rules below subtract from a site that otherwise serves everyone.
    ``allow_sources`` is not a whitelist — nginx's access module takes the first
    matching rule, and the template emits allows before denies, so an allow
    entry is an *exemption* from the deny entries under it. To close a site,
    deny ``0.0.0.0/0`` and ``::/0`` and list the exemptions in
    ``allow_sources``; there is no implicit trailing deny to depend on.

    The rules are evaluated by two different nginx phases and do not compose in
    the order they are written: ``denied_countries`` / ``allowed_countries`` and
    ``denied_methods`` compile to ``return`` in the rewrite phase, which runs
    before the access phase that ``allow`` / ``deny`` belong to. A country
    denial therefore wins over a source exemption. This is the safer of the two
    orderings, but it does mean ``allow_sources`` cannot be used to let an
    address bypass a country block.

    None of it applies to ``/.well-known/acme-challenge/``. Certificate
    issuance depends on that path being reachable from whichever address the CA
    validates from, and a site that locked itself out of renewal would fail
    weeks later, at expiry, with nothing connecting the outage to the rule that
    caused it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    #: ISO 3166-1 alpha-2, resolved from the client address by ngx_http_geoip2.
    #: Requires ``blitzecdn_nginx_geoip_enabled`` on the edge; the role refuses
    #: to converge a site that asks for country rules without it rather than
    #: rendering a config that silently admits everyone.
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    #: URI prefixes answered with 403 before the request reaches the origin.
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("allow_sources", "deny_sources")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalise to CIDR, rejecting a network with host bits set.

        ``strict=True`` is deliberate: ``ipaddress`` would happily read
        ``203.0.113.4/24`` as ``203.0.113.0/24``, turning a rule an operator
        wrote for one address into one covering 256 of them.
        """
        normalized = []
        for item in values:
            candidate = item.strip()
            try:
                normalized.append(str(ipaddress.ip_network(candidate, strict=True)))
            except ValueError as error:
                raise ValueError(
                    f"{item!r} is not an IP address or CIDR network: {error}"
                ) from error
        return unique(tuple(normalized), "source")

    @field_validator("allowed_countries", "denied_countries")
    @classmethod
    def validate_countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Reject codes ISO does not assign.

        An unassigned code renders happily and then matches nobody, so shape
        validation alone is not enough.
        """
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not COUNTRY_CODE.fullmatch(candidate):
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
                )
            if candidate in COUNTRY_ALIASES:
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code; "
                    f"use {COUNTRY_ALIASES[candidate]!r}"
                )
            if candidate not in ISO_3166_1_ALPHA_2:
                raise ValueError(
                    f"{item!r} is not an ISO 3166-1 alpha-2 country code such as 'DE'"
                )
            normalized.append(candidate)
        return unique(tuple(normalized), "country")

    @field_validator("denied_methods")
    @classmethod
    def validate_methods(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = []
        for item in values:
            candidate = item.strip().upper()
            if not HTTP_METHOD.fullmatch(candidate):
                raise ValueError(f"{item!r} is not an HTTP method such as 'DELETE'")
            normalized.append(candidate)
        return unique(tuple(normalized), "method")

    @field_validator("denied_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Accept a plain URI prefix and nothing that could end a directive.

        These are interpolated into a ``location`` in the generated config, so
        the characters nginx uses to close one — and the whitespace that would
        introduce a second argument — cannot appear.
        """
        normalized = []
        for item in values:
            candidate = item.strip()
            if not candidate.startswith("/"):
                raise ValueError(f"denied path {item!r} must start with '/'")
            if len(candidate) > 512:
                raise ValueError("denied paths must be at most 512 characters")
            if any(character in candidate for character in " \t\r\n;{}\"'\\"):
                raise ValueError(
                    f"denied path {item!r} may not contain whitespace, quotes, "
                    "backslashes, ';', '{' or '}'"
                )
            normalized.append(candidate)
        return unique(tuple(normalized), "path")

    @model_validator(mode="after")
    def validate_country_rules(self) -> Self:
        """Refuse a pair of country lists that can only deny everything.

        An allow list is emitted as "deny anything not in this list", so an
        entry in both lists is unreachable in one of them. Rather than pick a
        winner, say so — the operator meant one or the other.
        """
        overlap = set(self.allowed_countries) & set(self.denied_countries)
        if overlap:
            raise ValueError(
                "allowed_countries and denied_countries both list "
                + ", ".join(sorted(overlap))
            )
        return self

    @property
    def empty(self) -> bool:
        """True when this block imposes nothing, so it can be left unsent."""
        return not any(getattr(self, field) for field in SiteFirewall.model_fields)

    @property
    def requires_geoip(self) -> bool:
        return bool(self.allowed_countries or self.denied_countries)


class SitePolicy(BaseModel):
    """How a hostname is served once it is proxied.

    Declared once and inherited, because the same block appears three times:
    on ``CdnSite``, on the ``DnsRecord`` that derives it, and — as all-optional
    fields — on ``RecordPatch``. Adding a knob in one place and forgetting the
    others produces a setting an operator can set and never see applied, which
    is exactly the failure the contract test cannot catch. ``RecordPatch``
    cannot inherit (every field has to become optional), so ``test_domain.py``
    asserts its field names still match this class.

    Inheriting puts these fields ahead of the identity fields in ``model_dump``
    order. Nothing consumes them positionally — the desired-state document is a
    mapping — so only the shape of the generated YAML changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_port: int | None = Field(default=None, ge=1, le=65535)
    ssl_mode: SslMode = SslMode.OFF
    #: Redirect visitor HTTP requests to the same URI over HTTPS when this site
    #: serves TLS. Kept independent from ``ssl_mode`` so a site can offer both
    #: schemes while retaining its edge and origin encryption policy.
    always_use_https: bool = False
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool = True
    certificate_mode: CertificateMode = CertificateMode.DISABLED
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool = True
    cache_valid_success: str = "10m"
    cache_valid_not_found: str = "1m"
    #: Nested rather than flattened into six more fields, so a PATCH that
    #: touches the firewall replaces the whole block. Merging partial rule
    #: lists would make "remove the last deny" impossible to express.
    firewall: SiteFirewall = SiteFirewall()

    @field_validator("origin_request_host", "origin_sni")
    @classmethod
    def validate_optional_host(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None

    @field_validator("cache_valid_success", "cache_valid_not_found")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if not DURATION.fullmatch(value):
            raise ValueError(
                "duration must be a non-negative integer followed by "
                "ms, s, m, h, d, or w"
            )
        return value


class CdnSite(SitePolicy):
    """Validated, provider-independent desired state for one CDN virtual host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    server_names: tuple[str, ...] = Field(min_length=1, max_length=100)
    origin_host: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not SITE_NAME.fullmatch(normalized):
            raise ValueError(
                "name must start with a letter and contain only a-z, 0-9, and hyphens"
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

    @field_validator("certificate_path", "certificate_key_path")
    @classmethod
    def validate_remote_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError(
                "certificate paths must be absolute and cannot contain '..'"
            )
        if not value.startswith(_CERTIFICATE_ROOTS):
            raise ValueError(
                "certificate paths must live under one of: "
                + ", ".join(_CERTIFICATE_ROOTS)
            )
        return value

    @model_validator(mode="after")
    def validate_certificate_pair(self) -> Self:
        """Keep certificate mode and the two paths mutually consistent.

        Turning TLS off means clearing ``certificate_mode``, ``certificate_path``
        and ``certificate_key_path`` together in a single update; a patch that
        only sets the mode to ``disabled`` is rejected.
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
        if self.certificate_mode in {
            CertificateMode.UPLOADED,
            CertificateMode.REQUESTED,
        } and (self.certificate_path, self.certificate_key_path) != (
            managed_certificate_paths(self.name)
        ):
            raise ValueError(
                f"certificate_mode={self.certificate_mode.value!r} is set by the "
                "certificate upload and request endpoints, which own the paths "
                f"under {MANAGED_TLS_ROOT}/<site>/"
            )
        if (
            self.ssl_mode.serves_tls
            and self.certificate_mode is CertificateMode.DISABLED
        ):
            raise ValueError(
                f"ssl_mode={self.ssl_mode.value!r} requires an active edge certificate"
            )
        return self

    @property
    def effective_origin_sni(self) -> str:
        """The name sent as SNI when the origin is reached over HTTPS.

        A wildcard can never appear here. ``server_names`` may hold entries such
        as ``*.example.com`` — legal in an nginx ``server_name``, meaningless in
        a TLS handshake, where a literal ``*.example.com`` matches no
        certificate and the handshake fails or, with verification off, succeeds
        against whatever the origin happens to serve. So the fallback is
        ``origin_host``: the name we are actually connecting to, which is what
        the origin's certificate is expected to cover. ``origin_sni`` overrides
        it for an origin that presents something else, and both it and
        ``origin_request_host`` are validated without ``wildcard=True``.

        ``site.conf.j2`` computes ``proxy_ssl_name`` the same way; the two must
        agree or the preflight probe verifies a certificate the edge never asks
        for.
        """
        return self.origin_sni or self.origin_request_host or self.origin_host

    @property
    def serves_tls(self) -> bool:
        """Whether this site answers on 443.

        Cached entries include the request scheme in their key. When
        ``always_use_https`` is disabled, a TLS site can therefore cache its
        HTTP and HTTPS responses independently.
        """
        return self.ssl_mode.serves_tls

    def to_ansible(self) -> dict[str, Any]:
        document = self.model_dump(mode="json", exclude_none=True)
        # An untouched firewall is dropped rather than sent as six empty lists.
        # The desired state is read by operators during an incident, and a
        # block that appears on every site says nothing about the one site that
        # actually filters. The role defaults the key back to empty.
        if self.firewall.empty:
            del document["firewall"]
        return document
