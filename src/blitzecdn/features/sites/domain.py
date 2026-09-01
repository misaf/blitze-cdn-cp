"""The edge virtual host: one composition of every capability's site policy.

``CdnSite`` is the whole of what the control plane asks an edge to serve. DNS
records currently derive it, but the site contract does not depend on DNS.

This module *composes*; it does not own. Compression, HTTP protocol, security
and TLS policy each belong to the capability of the same name and are imported
from there. What lives here is what only exists once the fragments are on one
model: the rules that read across two capabilities at once — HTTP/3 needing
edge TLS, a certificate mode agreeing with its two paths, GeoIP being required
by either a country rule or a visitor header — and the site's identity.

The public model deliberately remains flat: API v1/v2, persisted policy JSON,
deployment snapshots, and Ansible all consume that shape. Composition is by
inheritance rather than by nesting for that reason alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.validation import SITE_NAME, hostname
from blitzecdn.features.compression.policy import CompressionMode, CompressionPolicy
from blitzecdn.features.http.policy import (
    DEFAULT_PORTS,
    HttpScheme,
    ProtocolPolicy,
)
from blitzecdn.features.security.policy import SecurityPolicy
from blitzecdn.features.sites.policy import (
    CachePolicy,
    HeaderPolicy,
    OriginPolicy,
)
from blitzecdn.features.tls.policy import (
    CERTIFICATE_ROOTS,
    MANAGED_TLS_ROOT,
    CertificateMode,
    SslAutomaticMode,
    TlsPolicy,
    managed_certificate_paths,
)

__all__ = ["CdnSite", "SitePolicy"]


class SitePolicy(
    TlsPolicy,
    ProtocolPolicy,
    OriginPolicy,
    CachePolicy,
    CompressionPolicy,
    SecurityPolicy,
    HeaderPolicy,
):
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

    enabled: bool = True

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Optional capability tokens, and the settings that asked for each.

        The single place a site's dependency on a detachable implementation is
        derived. One generic mechanism for every capability — there is no
        ``if capability == "geoip":`` anywhere downstream, and a token this
        repository has never heard of would travel the same path — so a new
        capability is one entry here rather than a check in a service.

        The settings travel with the token because "capability 'geoip' is not
        installed" alone leaves an operator hunting for which of two unrelated
        switches asked for it. Names are the stable schema's own field names,
        so what the message says to change is what a patch would set.

        A disabled site converges no server block, so it requests nothing.
        """
        if not self.enabled:
            return {}
        requested: dict[str, list[str]] = {}

        def request(capability: str, setting: str) -> None:
            requested.setdefault(capability, []).append(setting)

        if self.compression is not CompressionMode.OFF:
            request("compression", "compression")
        if self.under_attack_mode:
            request("security", "under_attack_mode")
        if not self.firewall.empty:
            request("security", "firewall")
        if self.certificate_mode in {
            CertificateMode.UPLOADED,
            CertificateMode.REQUESTED,
        }:
            request("certificates", "certificate_mode")
        elif (
            self.certificate_mode is not CertificateMode.DISABLED
            and self.ssl_automatic_mode is SslAutomaticMode.AUTO
        ):
            request("certificates", "ssl_automatic_mode")
        if self.http3_enabled:
            request("http3", "http3_enabled")
        for setting in self.geoip_settings:
            request("geoip", setting)
        return {name: tuple(settings) for name, settings in requested.items()}

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Optional implementation tokens requested by the stable schema."""
        return frozenset(self.capability_requirements)

    @model_validator(mode="after")
    def validate_http3_requires_edge_tls(self) -> Self:
        if self.http3_enabled and not self.ssl_mode.serves_tls:
            raise ValueError("http3_enabled=True requires ssl_mode to serve edge TLS")
        return self

    @property
    def geoip_settings(self) -> tuple[str, ...]:
        """Which settings on this site ask the edge to resolve a country.

        The one list, and the only place a country-aware setting is named.
        Country firewall rules were the first thing to need the GeoIP2 lookup
        and ``BZ-IPCountry`` is the second, so both consumers read this rather
        than asking again: ``requires_geoip`` is whether it is non-empty, and
        ``capability_requirements`` quotes it into the message an operator
        reads. A third consumer extends this tuple and gets both for free.

        Ordered as declared rather than as configured, so the message a fleet
        produces does not depend on which switch happened to be set first.
        """
        return tuple(
            name
            for name, requested in (
                ("firewall.allowed_countries", bool(self.firewall.allowed_countries)),
                ("firewall.denied_countries", bool(self.firewall.denied_countries)),
                ("visitor_headers.ip_country", self.visitor_headers.ip_country),
            )
            if requested
        )

    @property
    def requires_geoip(self) -> bool:
        """Whether serving this site needs ``$blitzecdn_country`` to exist.

        The single question the edge role's validation asks, and the same one
        ``capability_requirements`` turns into the ``geoip`` token — so the
        control plane refuses such a site before a playbook runs when the
        capability is not installed, and the role's assertion is the second
        line, for a desired state that did not come from here.
        """
        return bool(self.geoip_settings)


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
        if not value.startswith(CERTIFICATE_ROOTS):
            raise ValueError(
                "certificate paths must live under one of: "
                + ", ".join(CERTIFICATE_ROOTS)
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
        :class:`blitzecdn.features.edges.probe.OriginProbe` for why, and for
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
