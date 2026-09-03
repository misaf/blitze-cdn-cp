"""The edge virtual host: one composition of every capability's site policy.

``CdnSite`` is the whole of what the control plane asks an edge to serve, and
it is canonical: a site is created, edited and deleted here, not derived from
something else. What DNS contributes is ``server_names`` — the hostnames whose
records route to this site — and nothing more. Every other fact on the model
has exactly one writer, which is this capability.

This module *composes*; it does not own. Cache, compression, HTTP protocol,
security and TLS policy each belong to the capability of the same name and are
imported from there, and each answers for its own capability requirements —
this module merges them without naming one. What is left is what only exists
once the fragments are on one model: the rules that read across two
capabilities at once — HTTP/3 needing edge TLS, a certificate mode agreeing
with its two paths — and the site's identity.

``OriginPolicy`` and ``HeaderPolicy`` stay under ``sites/policy/`` because no
capability owns them. Setting a ``Host`` header on the origin leg and writing
the trusted ``BZ-*`` headers are things a managed edge does with nothing
installed beside the control plane; there is no distribution to reunite them
with, and inventing one to hold two value types would be the mirror of the
problem this arrangement fixed.

The public model deliberately remains flat: API v1/v2, persisted policy JSON,
deployment snapshots, and Ansible all consume that shape. Composition is by
inheritance rather than by nesting for that reason alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Self, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.cache.policy import CachePolicy, CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode, CompressionPolicy
from blitzecdn.capabilities.http.policy import (
    DEFAULT_PORTS,
    HttpScheme,
    ProtocolPolicy,
)
from blitzecdn.capabilities.security.policy import SecurityPolicy, SiteFirewall
from blitzecdn.capabilities.sites.policy import (
    HeaderPolicy,
    OriginPolicy,
    SiteVisitorHeaders,
)
from blitzecdn.capabilities.tls.policy import (
    CERTIFICATE_ROOTS,
    MANAGED_TLS_ROOT,
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
    managed_certificate_paths,
)
from blitzecdn.core.policy import CapabilityPolicy
from blitzecdn.core.validation import SITE_NAME, hostname

__all__ = ["CdnSite", "SitePatch", "SitePolicy"]


class SitePolicy(
    TlsPolicy,
    ProtocolPolicy,
    OriginPolicy,
    CachePolicy,
    CompressionPolicy,
    SecurityPolicy,
    HeaderPolicy,
):
    """How a hostname is served once a site answers for it.

    Declared once and inherited, because the same block appears twice: on
    ``CdnSite`` and — as all-optional fields — on ``SitePatch``. Adding a knob
    to one and forgetting the other produces a setting an operator can set once
    and never change again, which is exactly the failure the contract test
    cannot catch. ``SitePatch`` cannot inherit (every field has to become
    optional), so ``_assert_patch_covers_policy`` below refuses to import a
    version of this module where the two have drifted apart.

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
        *answered*, and no longer the place any of it is decided: each contract
        declares its own, and this merges them. There is no capability name in
        this module at all — not in a branch, not in a token, not in a list of
        contracts — so a capability this repository has never heard of travels
        the same path as `compression`, and a new one is a property on its own
        contract rather than a second edit here.

        The contracts are read off the MRO rather than listed, for that reason:
        what a list produces when someone forgets it is a token that is
        silently never requested, and a deployment that proceeds without the
        distribution that was supposed to serve it.

        The settings travel with the token because "capability 'geoip' is not
        installed" alone leaves an operator hunting for which of two unrelated
        switches asked for it. Names are the stable schema's own field names,
        so what the message says to change is what a patch would set. Two
        contracts may ask for one token and both sets of names survive; order
        follows the bases as declared, so the message does not depend on which
        switch happened to be set first.

        A disabled site converges no server block, so it requests nothing.
        """
        if not self.enabled:
            return {}
        requested: dict[str, list[str]] = {}
        for contract in type(self).__mro__:
            if contract in (SitePolicy, CapabilityPolicy):
                continue
            declared = contract.__dict__.get("capability_requirements")
            if not isinstance(declared, property) or declared.fget is None:
                continue
            for token, settings in declared.fget(self).items():
                requested.setdefault(token, []).extend(settings)
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
    def requires_geoip(self) -> bool:
        """Whether serving this site needs ``$blitzecdn_country`` to exist.

        Asked of the two contracts that can answer it rather than derived from
        a token this module names: a country firewall rule needs the lookup and
        so does ``BZ-IPCountry``, and each says so on its own model. The list of
        country-aware settings that used to live here — and the ``geoip``
        string beside it — went with them.

        Ungated by ``enabled`` on purpose. This is what the edge role's
        validation asks of a document it was handed; whether the site converges
        at all is a separate question, and ``capability_requirements`` is the
        one that asks it.
        """
        return self.firewall.requires_geoip or self.visitor_headers.requires_geoip


class CdnSite(SitePolicy):
    """Validated, provider-independent desired state for one CDN virtual host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: The hostnames this site answers on. Not set here: every entry is the
    #: fqdn of a DNS record routed to this site, and `dns` maintains the list
    #: as records come and go. Empty is legal and means the site is configured
    #: but not yet reachable — it contributes no server block, exactly as an
    #: unproxied record used to leave no trace, so an empty `server_name` can
    #: never reach nginx and turn the site into the default server.
    server_names: tuple[str, ...] = Field(default=(), max_length=100)
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
    def serves_traffic(self) -> bool:
        """Whether this site should contribute a server block at all.

        A site with no hostnames is configuration waiting for a DNS record to
        route to it. Rendering it would emit a ``server`` block with an empty
        ``server_name``, which nginx reads as the default server for the
        listener — so the site with the least configuration behind it would
        start answering for every hostname nobody else claimed. It is left out
        instead, which is what an unproxied record used to achieve by deriving
        no site at all.

        ``enabled`` is a separate switch and stays separate: a disabled site
        still occupies its name and its hostnames, and turning it back on is
        one field rather than a re-pointed record.
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
        :class:`blitzecdn.capabilities.edges.probe.OriginProbe` for why, and for
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


class SitePatch(BaseModel):
    """A partial update to a site: every field optional, unset means untouched.

    This cannot inherit ``SitePolicy`` — each field has to become optional, and
    an inherited required field would silently gain a default here. It is
    written out instead, and ``_assert_patch_covers_policy`` below refuses to
    import a version of this module where the two have drifted apart.

    Generating these fields with ``create_model`` would remove the duplication
    outright, but the generated class is opaque to mypy — every ``SitePatch``
    field access in the API and the CLI would stop being type-checked. Keeping
    the fields visible and checking the parity at import buys the same
    guarantee without giving up static checking.

    ``server_names`` is deliberately absent. It is the one part of a site this
    capability does not write: `dns` maintains it from the records routed here, so
    a patch that could set it would be the second writer of the only field that
    has another one.
    """

    model_config = ConfigDict(extra="forbid")

    origin_host: str | None = None
    ssl_mode: SslMode | None = None
    ssl_automatic_mode: SslAutomaticMode | None = None
    minimum_tls_version: MinimumTlsVersion | None = None
    http3_enabled: bool | None = None
    always_use_https: bool | None = None
    under_attack_mode: bool | None = None
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool | None = None
    certificate_mode: CertificateMode | None = None
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool | None = None
    cache_query_string_mode: CacheQueryStringMode | None = None
    cache_valid_success: str | None = None
    cache_valid_not_found: str | None = None
    compression: CompressionMode | None = None
    # Replaces the block wholesale; see the note on SitePolicy.firewall. Send
    # {"firewall": {}} to clear every rule.
    firewall: SiteFirewall | None = None
    # Replaced wholesale as well. Sending {"visitor_headers": {}} restores the
    # defaults rather than leaving the current switches in place.
    visitor_headers: SiteVisitorHeaders | None = None


def _without_none(annotation: object) -> object:
    """``T`` from ``T | None``, so a patch field and its policy field compare.

    Applied to both sides rather than only to the patch. Several policy fields
    are themselves optional — ``origin_sni`` is ``str | None`` on the site as
    well as on the patch — and stripping ``None`` from just one side would
    report every one of them as a type mismatch, which is how a check like this
    ends up deleted for crying wolf. What survives is the question worth asking:
    do the two agree on the type once "unset" is set aside.

    ``Optional[T]`` is ``Union[T, None]`` at runtime whichever spelling was
    used, so this reads the union's arms rather than the syntax.
    """
    if get_origin(annotation) is not UnionType and get_origin(annotation) is not Union:
        return annotation
    arms = [arm for arm in get_args(annotation) if arm is not type(None)]
    return arms[0] if len(arms) == 1 else annotation


def _assert_patch_covers_policy() -> None:
    """Refuse to import if a policy knob cannot be patched, or patched wrongly.

    Runs at import rather than only under pytest. The failures this guards
    against — a setting an operator can set on a site and never change again,
    or one whose patch takes a different type than the site stores — are silent
    everywhere else, so the process should not start with either.

    Three checks, because there are three ways to drift: a field can be absent,
    it can be present but required (an unset field would then stop meaning
    "untouched"), or it can be present and optional while carrying a type the
    site will refuse. The last one is why this is not just a name comparison: a
    policy field widened from ``int`` to ``int | str`` and not widened here
    fails only when an operator finally sends the new form.
    """
    missing = sorted(set(SitePolicy.model_fields) - set(SitePatch.model_fields))
    if missing:
        raise RuntimeError(
            "SitePatch is missing SitePolicy fields: "
            + ", ".join(missing)
            + ". Add them as optional, defaulting to None, or an operator can "
            "set them once and never change them."
        )
    required = sorted(
        name
        for name in SitePolicy.model_fields
        if SitePatch.model_fields[name].default is not None
    )
    if required:
        raise RuntimeError(
            "SitePatch fields must default to None so an unset field means "
            "'untouched'; these do not: " + ", ".join(required)
        )
    mistyped = sorted(
        f"{name} (site stores {policy.annotation}, patch takes "
        f"{SitePatch.model_fields[name].annotation})"
        for name, policy in SitePolicy.model_fields.items()
        if _without_none(SitePatch.model_fields[name].annotation)
        != _without_none(policy.annotation)
    )
    if mistyped:
        raise RuntimeError(
            "every SitePatch field must accept exactly what the site stores, "
            "widened only with None; these do not: " + ", ".join(mistyped)
        )


_assert_patch_covers_policy()
