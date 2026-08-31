"""The edge virtual host, and the document the edge roles consume.

``CdnSite`` is the whole of what the control plane asks an edge to serve. It is
derived rather than authored — :mod:`blitzecdn.domain.dns` owns the records it
comes from — so nothing here reads or writes state; it validates a shape and
renders it.

TLS, compression, caching, the firewall and the visitor headers all live in this
one module on purpose. They are not five subjects that happen to share a file:
they are the fields of ``SitePolicy``, which ``CdnSite`` inherits and
``RecordPatch`` mirrors, and they change together — a new knob is a field, a
validator, and a line in the desired-state contract, added in one edit. Splitting
them per feature would turn that single edit into a walk through five modules and
buy nothing, because none of them is independently importable: the value objects
exist to be fields of the policy, and the policy exists to be the base of the
site. The cross-field rules are the evidence — ``http3_enabled`` is refused
unless ``ssl_mode`` serves edge TLS, and ``certificate_mode`` has to stay
consistent with the two certificate paths — and a rule that reads two features
cannot live in either one's module.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Self

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


#: The Cloudflare-compatible public proxy ports, which are the edge's listeners.
#: They are two independent sets and not pairs: 8080 is an HTTP port and 8443 an
#: HTTPS one, and neither is the other's counterpart. Nothing here maps one onto
#: the other, and ``always_use_https`` deliberately does not either.
#:
#: These are the same lists as ``blitzecdn_edge_runtime.listeners.http`` and
#: ``.https`` in the edge runtime contract, which the Nginx role binds and the
#: firewall opens; ``test_contract.py`` holds the two copies together.
HTTP_PROXY_PORTS = (80, 8080, 8880, 2052, 2082, 2086, 2095)
HTTPS_PROXY_PORTS = (443, 2053, 2083, 2087, 2096, 8443)

#: The default port of each scheme. The canonical endpoint of a site is its
#: canonical scheme on this port, and it is the only one anything outside the
#: edge addresses — see :class:`blitzecdn.infrastructure.origins.OriginProbe`.
DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}


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
        """The scheme the edge uses toward the origin for one visitor request.

        The mode alone does not decide this, which is why it is a method and
        not a property: the answer depends on how the visitor arrived, both on
        which scheme and — for Flexible — on which port.

        ``full`` and ``full_strict`` mean *mirror the visitor*: a site in Full
        serves plain HTTP on its HTTP listeners exactly as it always did, and
        encrypts the origin leg only for the visitors who arrived encrypted.
        Terminating an HTTP request and re-originating it as HTTPS is not what
        either mode promises, and it breaks every origin that answers only one
        of the two.

        ``flexible`` is the Cloudflare-compatible exception, and the reason
        ``visitor_port`` is a parameter. Cloudflare supports Flexible only for
        HTTPS on 443; an HTTPS request on one of the five alternate HTTPS proxy
        ports falls back to Full-like transport and is proxied over HTTPS on
        that same port. Only the transport falls back — ``verifies_origin``
        stays false, so the fallback leg is Full and never Full (strict).

        ``off`` never encrypts anything, whichever scheme or port the request
        arrived on. (It serves no HTTPS listener at all, so its HTTPS rows
        cannot arise in practice; the method still answers for them.)

        The *port* is never chosen here — see the module note in
        ``site.conf.j2``. The visitor's destination port is preserved toward the
        origin, so it comes from the listener rather than from this decision.
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


class CacheQueryStringMode(StrEnum):
    """Whether request query strings distinguish cached responses."""

    INCLUDE = "include"
    IGNORE = "ignore"


class CompressionMode(StrEnum):
    """Which encodings the edge is willing to produce for itself.

    Ordered by what a client can be offered, not by preference: ``BROTLI``
    means Brotli *and* gzip, because a client that cannot decode ``br`` still
    sends ``gzip`` and would otherwise be handed an identity body. There is no
    "gzip only after Brotli" state to express — the two are advertised
    independently by every client that supports either.

    The ABI-matched Brotli filter is an invariant of BlitzeCDN's managed edge
    stack. A missing module fails provisioning instead of silently changing
    this policy to gzip.

    This governs what *this edge* compresses, not what a client receives. A
    response the origin already encoded arrives with ``Content-Encoding`` set
    and is passed through untouched under every mode including ``OFF``; nginx
    never re-compresses an encoded body, and stripping one to honour ``OFF``
    would mean decoding and re-encoding every hit to serve fewer bytes of
    nothing.
    """

    OFF = "off"
    GZIP = "gzip"
    BROTLI = "brotli"


class CertificateMode(StrEnum):
    DISABLED = "disabled"
    EXISTING = "existing"
    UPLOADED = "uploaded"
    REQUESTED = "requested"


class SiteVisitorHeaders(BaseModel):
    """The ``BZ-*`` request headers the edge adds on the way to the origin.

    These are edge-owned metadata, not a relay of what the visitor sent. Every
    header named here is written by nginx from a value only the edge can know —
    the connection's own peer address, and the country the GeoIP2 database
    resolves it to — and a visitor-supplied header of the same name is replaced
    on the way through. A disabled header is not merely unset: the template
    clears it, so the ``BZ-`` namespace never carries anything a client wrote.

    That is the whole security model, and it depends on one thing being true at
    the origin: the origin must accept traffic only from BlitzeCDN edges. A
    ``BZ-Connecting-IP`` arriving on a connection that did not come from an edge
    means nothing, exactly as ``X-Forwarded-For`` does.

    ``ip_country`` needs ngx_http_geoip2 and a MaxMind database. Unlike Brotli,
    which degrades, a site that asks for it on an edge without GeoIP fails the
    converge: the alternative is an origin reading an absent header as "country
    unknown" and taking a decision on it. The role refuses rather than omit the
    header — see ``requires_geoip`` and the role's validation tasks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``BZ-Connecting-IP``: the visitor address, IPv4 or IPv6, as nginx saw it
    #: on the connection. On by default — an origin behind the CDN sees an edge
    #: address on every connection and has no other way to learn the visitor's.
    connecting_ip: bool = True
    #: ``BZ-IPCountry``: the ISO 3166-1 alpha-2 code for the visitor address.
    #: Off by default, because it cannot be honoured without GeoIP and the edge
    #: role has GeoIP off until an operator turns it on; defaulting it on would
    #: fail the next converge of every existing site.
    ip_country: bool = False

    @property
    def requires_geoip(self) -> bool:
        return self.ip_country


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
    #: Requires ``blitzecdn_edge_geoip_enabled`` on the edge; the role refuses
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

    ssl_mode: SslMode = SslMode.OFF
    #: Cloudflare-compatible enrollment in Automatic SSL/TLS. Automatic scans
    #: may only move ``ssl_mode`` toward stronger encryption; Custom leaves the
    #: value entirely under operator control.
    ssl_automatic_mode: SslAutomaticMode = SslAutomaticMode.AUTO
    #: Cloudflare-compatible edge minimum. TLS 1.2 preserves the existing
    #: browser compatibility; 1.3 can be selected per hostname.
    minimum_tls_version: MinimumTlsVersion = MinimumTlsVersion.TLS_1_2
    #: Offer HTTP/3 over QUIC to visitors on UDP/443. QUIC always negotiates
    #: TLS 1.3 itself; this does not raise the minimum used by the parallel TCP
    #: HTTPS listeners, which may continue accepting TLS 1.2.
    http3_enabled: bool = False
    #: Redirect visitor HTTP requests to the same URI over HTTPS when this site
    #: serves TLS. Kept independent from ``ssl_mode`` so a site can offer both
    #: schemes while retaining its edge and origin encryption policy.
    always_use_https: bool = False
    #: Emergency browser verification at the edge. The Nginx role refuses to
    #: serve a site carrying this policy unless its njs capability and signing
    #: secret are explicitly enabled for the fleet.
    under_attack_mode: bool = False
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool = True
    certificate_mode: CertificateMode = CertificateMode.DISABLED
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool = True
    #: Include is the safe default: query parameters often select genuinely
    #: different content. Ignore deliberately collapses every query variant of
    #: the same raw path onto one cached object.
    cache_query_string_mode: CacheQueryStringMode = CacheQueryStringMode.INCLUDE
    cache_valid_success: str = "10m"
    cache_valid_not_found: str = "1m"
    #: Brotli by default, matching the Cloudflare-compatible posture: it is the
    #: better encoding where both ends have it, and it degrades to gzip on an
    #: edge that does not. Edges already key the cache by normalised
    #: ``Accept-Encoding``, so compressing here cannot serve a body a client
    #: cannot decode.
    compression: CompressionMode = CompressionMode.BROTLI
    #: Nested rather than flattened into six more fields, so a PATCH that
    #: touches the firewall replaces the whole block. Merging partial rule
    #: lists would make "remove the last deny" impossible to express.
    firewall: SiteFirewall = SiteFirewall()
    #: Nested for the same reason, and because the two switches are one
    #: subject: what the edge tells the origin about the visitor.
    visitor_headers: SiteVisitorHeaders = SiteVisitorHeaders()

    @model_validator(mode="after")
    def validate_http3_requires_edge_tls(self) -> Self:
        if self.http3_enabled and not self.ssl_mode.serves_tls:
            raise ValueError("http3_enabled=True requires ssl_mode to serve edge TLS")
        return self

    @property
    def requires_geoip(self) -> bool:
        """Whether serving this site needs ``$blitzecdn_country`` to exist.

        The single question the edge role's validation asks. Country firewall
        rules were the first thing to need the GeoIP2 lookup and
        ``BZ-IPCountry`` is the second, so the answer is composed here rather
        than asked twice — a third consumer must extend this property, not add
        a parallel condition to the role.
        """
        return self.firewall.requires_geoip or self.visitor_headers.requires_geoip

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

    @property
    def canonical_visitor_scheme(self) -> HttpScheme:
        """The strongest scheme a visitor can reach this site on.

        HTTPS for every mode that serves TLS, HTTP for Off. This is the entry
        the site is *for*: an HTTP listener on a TLS site either redirects
        (``always_use_https``) or serves the same content unencrypted.
        """
        return HttpScheme.HTTPS if self.serves_tls else HttpScheme.HTTP

    @property
    def canonical_origin_scheme(self) -> HttpScheme:
        """The origin scheme for a canonical visitor — what preflight probes.

        The edge preserves the visitor's port toward the origin, so a site has
        one origin endpoint per public proxy port rather than one overall.
        Preflight deliberately checks only this one; see
        :class:`blitzecdn.infrastructure.origins.OriginProbe` for why, and for
        what it means for a site whose origin serves only 80 and 443.

        Under Flexible this is the HTTP origin, because the canonical HTTPS
        listener is 443 and Flexible on 443 does not encrypt the origin leg.
        The same site's alternate HTTPS listeners fall back to an HTTPS origin
        leg, which this endpoint says nothing about — again, see ``OriginProbe``.
        """
        scheme = self.canonical_visitor_scheme
        return self.ssl_mode.origin_scheme_for(scheme, DEFAULT_PORTS[scheme])

    @property
    def redirects_http_to_https(self) -> bool:
        """Whether the HTTP listeners redirect instead of proxying.

        ``always_use_https`` is inert unless the site actually serves TLS, and
        that is the whole of BlitzeCDN's answer to the ``ssl_mode=off`` case.
        Cloudflare removes the Always Use HTTPS control from the dashboard while
        the encryption mode is Off — the stored preference survives and takes
        effect again when a secure mode is selected, but nothing redirects in
        the meantime. Off serves no HTTPS listener, so honouring the flag there
        would send every visitor to a port the edge does not answer on.

        Modelling it here rather than rejecting the combination keeps the record
        API unchanged and lets an operator set the two in either order.
        ``site.conf.j2`` gates its redirect on the identical condition, and
        ``test_contract.py`` renders the template against this property so the
        two cannot drift.
        """
        return self.serves_tls and self.always_use_https
