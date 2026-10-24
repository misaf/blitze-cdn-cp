"""Effective virtual hosts, derived from a zone, its rules, and its records.

DNS owns those canonical aggregates. This model describes what an edge serves
and is shared by compilation, certificates, origin checks, and deployments.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.http.policy import DEFAULT_PORTS, HttpScheme
from blitzecdn.capabilities.serving.policy import SitePolicy
from blitzecdn.capabilities.tls.policy import (
    MANAGED_TLS_ROOT,
    CertificateMode,
    CertificateVersion,
)
from blitzecdn.core.domain.validation import HOST_NAME, hostname


class CdnSite(BaseModel):
    """Validated, provider-independent desired state for one CDN virtual host.

    Derived, and carrying no record of what it was derived from. A certificate
    issued for these hostnames does have to be written back to the zone or the
    rule that produced them — but ``name`` already says which, because
    :func:`~blitzecdn.capabilities.dns.domain.hosts.host_name` is a function of
    exactly those two things. Storing the answer beside the name would be a
    second copy of it, free to disagree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    policy: SitePolicy = Field(default_factory=SitePolicy)
    #: The hostnames this virtual host answers on: every proxied record whose
    #: fqdn resolved to this policy. Empty is legal and means a zone whose
    #: hostnames are all unproxied — it contributes no server block, so an
    #: empty `server_name` can never reach nginx and make this the default
    #: server for the listener.
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
        """Keep certificate mode and the two paths mutually consistent.

        Turning TLS off means clearing ``certificate_mode``, ``certificate_path``
        and ``certificate_key_path`` together in a single update; a patch that
        only sets the mode to ``disabled`` is rejected.
        """
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

        A zone whose hostnames are all unproxied derives one of these with no
        hostnames on it. Rendering it would emit a ``server`` block with an
        empty ``server_name``, which nginx reads as the default server for the
        listener — so the policy with the least behind it would start answering
        for every hostname nobody else claimed.

        ``enabled`` is a separate switch and stays separate: a zone turned off
        keeps its policy and its hostnames, and turning it back on is one field
        rather than a re-proxied record.
        """
        return bool(self.server_names)

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
        return (
            self.policy.origin.origin_sni
            or self.policy.origin.origin_request_host
            or self.origin_host
        )

    @property
    def serves_tls(self) -> bool:
        """Whether this site answers on 443.

        Cached entries include the request scheme in their key. When
        ``always_use_https`` is disabled, a TLS site can therefore cache its
        HTTP and HTTPS responses independently.
        """
        return self.policy.tls.ssl_mode.serves_tls

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
        :class:`blitzecdn.capabilities.edges.adapters.probe.OriginProbe` for
        why, and for what it means for a site serving only 80 and 443.

        Under Flexible this is the HTTP origin, because the canonical HTTPS
        listener is 443 and Flexible on 443 does not encrypt the origin leg.
        The same site's alternate HTTPS listeners fall back to an HTTPS origin
        leg, which this endpoint says nothing about — again, see ``OriginProbe``.
        """
        scheme = self.canonical_visitor_scheme
        return self.policy.tls.ssl_mode.origin_scheme_for(scheme, DEFAULT_PORTS[scheme])

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
        the edge-contract suite renders the template against this property so the
        two cannot drift.
        """
        return self.serves_tls and self.policy.tls.always_use_https
