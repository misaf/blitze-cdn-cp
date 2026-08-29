"""Zones, records, and the derivation of an edge site from a record.

Records are the source of truth. A site exists because a record is proxied, and
``DnsRecord.to_site`` is the whole of that mapping — which is why this module
imports :mod:`blitzecdn.domain.sites` and never the other way round.
"""

from __future__ import annotations

import hashlib
import ipaddress
from enum import StrEnum
from types import UnionType
from typing import Self, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.domain.sites import (
    CacheQueryStringMode,
    CdnSite,
    CertificateMode,
    CompressionMode,
    MinimumTlsVersion,
    SiteFirewall,
    SitePolicy,
    SiteVisitorHeaders,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.domain.validation import DNS_LABEL, hostname


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"


class Domain(BaseModel):
    """A DNS zone a customer has delegated to us.

    Holds no records itself — they are stored separately and keyed by domain,
    so a zone with a thousand records is not rewritten to change one of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = hostname(value)
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            if "." not in normalized:
                raise ValueError(
                    "domain must be a delegable zone such as 'example.com'"
                ) from None
            return normalized
        raise ValueError("domain must be a name, not an IP address")


def derive_site_name(fqdn: str) -> str:
    """Return the ``CdnSite.name`` a proxied record derives.

    Site names are an internal identifier constrained to ``SITE_NAME``, which
    admits neither dots nor ``*``. The mapping has to be deterministic: the
    certificate store is keyed by site name (``infrastructure/certificates.py``),
    so a name that changed between runs would orphan a live certificate.

    Collisions are possible in principle — ``a.b.example.com`` and
    ``a-b.example.com`` both flatten to ``a-b-example-com`` — so
    ``ControlPlane.validate()`` rejects two records that derive the same name
    rather than letting one silently overwrite the other.
    """
    slug = fqdn.replace("*", "wildcard").replace(".", "-")
    if not slug[:1].isalpha():
        slug = f"x-{slug}"
    if len(slug) > 63:
        digest = hashlib.blake2s(fqdn.encode("utf-8"), digest_size=4).hexdigest()[:7]
        slug = f"{slug[:55]}-{digest}"
    return slug


class DnsRecord(SitePolicy):
    """One record in a zone, and the CDN policy for it when proxied.

    ``proxied`` is the CDN on/off switch. Proxied, the edge serves this
    hostname and ``value`` is the origin it fetches from. Unproxied, the edge
    does not know the hostname exists and ``value`` is simply what DNS answers
    with — the record still belongs to us, it just bypasses the proxy.

    The CDN fields below are only meaningful while ``proxied`` is true. They
    are kept rather than cleared when it is turned off, so flipping the switch
    back does not silently lose a cache or TLS setting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    type: RecordType = RecordType.A
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    proxied: bool = False

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return hostname(value)

    @field_validator("name")
    @classmethod
    def validate_record_name(cls, value: str) -> str:
        """``@`` is the zone apex, ``*`` a wildcard, anything else a subdomain."""
        normalized = value.strip().lower().rstrip(".")
        if normalized in {"@", "*"}:
            return normalized
        if not normalized:
            raise ValueError("record name cannot be empty; use '@' for the apex")
        if not all(DNS_LABEL.fullmatch(label) for label in normalized.split(".")):
            raise ValueError(f"invalid record name: {value!r}")
        return normalized

    @model_validator(mode="after")
    def validate_value_matches_type(self) -> Self:
        try:
            address = ipaddress.ip_address(self.value.strip())
        except ValueError:
            raise ValueError(
                f"{self.type.value} record value must be an IP address"
            ) from None
        expected = 4 if self.type is RecordType.A else 6
        if address.version != expected:
            raise ValueError(
                f"{self.type.value} record value must be an IPv{expected} address"
            )
        return self

    @model_validator(mode="after")
    def validate_the_site_it_derives(self) -> Self:
        """Reject a proxied record that cannot produce a valid site.

        ``CdnSite`` owns the certificate-path rules that keep a deploy from
        writing as root outside ``/etc/blitzecdn/tls``, ``/etc/ssl`` and
        ``/etc/letsencrypt``. Deriving here means a record carrying a path
        outside them is refused when it is written, rather than persisting and
        then failing later when the derived table is rebuilt.
        """
        self.to_site()
        return self

    @property
    def fqdn(self) -> str:
        """The hostname this record answers for."""
        if self.name == "@":
            return self.domain
        return f"{self.name}.{self.domain}"

    @property
    def site_name(self) -> str:
        return derive_site_name(self.fqdn)

    def to_site(self) -> CdnSite | None:
        """Derive the edge virtual host, or ``None`` when the CDN is bypassed.

        Returning ``None`` is what takes the hostname off the edge entirely.
        The caller must not substitute a disabled site: a disabled site still
        occupies its name, whereas an unproxied record should leave no trace in
        the desired state at all.
        """
        if not self.proxied:
            return None
        # Copied by name off the shared policy rather than listed out, so a new
        # setting reaches the edge the moment it is added to ``SitePolicy``.
        return CdnSite(
            name=self.site_name,
            server_names=(self.fqdn,),
            origin_host=self.value,
            **{field: getattr(self, field) for field in SitePolicy.model_fields},
        )


class RecordPatch(BaseModel):
    """A partial update to a record: every field optional, unset means untouched.

    This cannot inherit ``SitePolicy`` — each field has to become optional, and
    an inherited required field would silently gain a default here. It is
    written out instead, and ``_assert_patch_covers_policy`` below refuses to
    import a version of this module where the two have drifted apart.

    Generating these fields with ``create_model`` would remove the duplication
    outright, but the generated class is opaque to mypy — every ``RecordPatch``
    field access in the API and the CLI would stop being type-checked. Keeping
    the fields visible and checking the parity at import buys the same
    guarantee without giving up static checking.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None
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
    are themselves optional — ``origin_sni`` is ``str | None`` on the record as
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
    against — a setting an operator can set on a record and never change again,
    or one whose patch takes a different type than the record stores — are
    silent everywhere else, so the process should not start with either.

    Three checks, because there are three ways to drift: a field can be absent,
    it can be present but required (an unset field would then stop meaning
    "untouched"), or it can be present and optional while carrying a type the
    record will refuse. The last one is why this is not just a name comparison:
    a policy field widened from ``int`` to ``int | str`` and not widened here
    fails only when an operator finally sends the new form.
    """
    missing = sorted(set(SitePolicy.model_fields) - set(RecordPatch.model_fields))
    if missing:
        raise RuntimeError(
            "RecordPatch is missing SitePolicy fields: "
            + ", ".join(missing)
            + ". Add them as optional, defaulting to None, or an operator can "
            "set them once and never change them."
        )
    required = sorted(
        name
        for name in SitePolicy.model_fields
        if RecordPatch.model_fields[name].default is not None
    )
    if required:
        raise RuntimeError(
            "RecordPatch fields must default to None so an unset field means "
            "'untouched'; these do not: " + ", ".join(required)
        )
    mistyped = sorted(
        f"{name} (record stores {policy.annotation}, patch takes "
        f"{RecordPatch.model_fields[name].annotation})"
        for name, policy in SitePolicy.model_fields.items()
        if _without_none(RecordPatch.model_fields[name].annotation)
        != _without_none(policy.annotation)
    )
    if mistyped:
        raise RuntimeError(
            "every RecordPatch field must accept exactly what the record "
            "stores, widened only with None; these do not: " + ", ".join(mistyped)
        )


_assert_patch_covers_policy()
