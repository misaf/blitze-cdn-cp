"""Partial zone policy updates, also used to validate rule overrides.

Unset fields leave stored values unchanged. The complete zone is validated
after merging a patch so that constraints across fields are checked together.
``_assert_patch_covers_zone`` runs at import time to reject field or type drift
between the zone and its patch model."""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.domain.zone import Domain
from blitzecdn.capabilities.dns.policy import SiteVisitorHeaders
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)

__all__ = ["DomainPatch", "reject_issuer_owned_certificate"]

#: Set by adding a zone, changed by deleting one. The records and the rules
#: hang off the name, so a patch that could set it would be a rename that
#: silently orphaned both.
_NOT_PATCHABLE = frozenset({"name"})


def reject_issuer_owned_certificate(changes: Mapping[str, Any]) -> None:
    """Refuse an operator edit that claims controller-managed TLS material.

    Called from the two places an operator authors policy — updating a zone,
    and authoring a rule's overrides — and from neither of the writes the
    certificates capability makes, which reach the store through
    ``replace_domain`` and ``replace_rule`` without passing here.

    The check cannot live on ``DomainPatch`` itself even though both callers
    speak in patches, because ``HostService.activate_managed_certificate``
    records a rule's issued certificate *as* an overrides mapping, and that
    mapping is the one thing that legitimately carries ``requested``. Nor can
    it live on ``Domain``: a zone holds the mode perfectly well once the issuer
    has written it. What is being checked is not the value but who is setting
    it, and the entry point is the only place that knows.

    Without it the mode is merely *inconsistent* rather than refused, and the
    failure is quiet: ``CdnSite`` rejects a controller-managed mode whose paths
    are not the ones derived from the host's name, ``derive_hosts`` drops the
    host it could not build instead of raising, and the zone's hostnames stop
    being served until ``blitzecdn validate`` names them.
    """
    mode = changes.get("certificate_mode")
    if mode is None:
        return
    if CertificateMode(mode).issuer_owned:
        raise ValueError(
            f"certificate_mode={CertificateMode(mode).value!r} is set by the "
            "certificate upload and request endpoints, which own the material "
            "and the paths it lives under; an operator sets 'disabled' or "
            "'existing'"
        )


class DomainPatch(BaseModel):
    """A partial update to a zone: every field optional, unset means untouched.

    This cannot inherit ``Domain`` — each field has to become optional, and an
    inherited required field would silently gain a default here. It is written
    out instead, and ``_assert_patch_covers_zone`` below refuses to import a
    version of this module where the two have drifted apart.

    Generating these fields with ``create_model`` would remove the duplication
    outright, but the generated class is opaque to mypy — every ``DomainPatch``
    field access in the API and the CLI would stop being type-checked. Keeping
    the fields visible and checking the parity at import buys the same
    guarantee without giving up static checking.
    """

    model_config = ConfigDict(extra="forbid")

    ssl_mode: SslMode | None = None
    ssl_automatic_mode: SslAutomaticMode | None = None
    minimum_tls_version: MinimumTlsVersion | None = None
    http3_enabled: bool | None = None
    max_upload_size: MaxUploadSize | None = None
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
    """``T`` from ``T | None``, so a patch field and its zone field compare.

    Applied to both sides rather than only to the patch. Several zone fields
    are themselves optional — ``origin_sni`` is ``str | None`` on the zone as
    well as on the patch — and stripping ``None`` from just one side would
    report every one of them as a type mismatch, which is how a check like this
    ends up deleted for crying wolf. What survives is the question worth
    asking: do the two agree on the type once "unset" is set aside.

    ``Optional[T]`` is ``Union[T, None]`` at runtime whichever spelling was
    used, so this reads the union's arms rather than the syntax.
    """
    if get_origin(annotation) is not UnionType and get_origin(annotation) is not Union:
        return annotation
    arms = [arm for arm in get_args(annotation) if arm is not type(None)]
    return arms[0] if len(arms) == 1 else annotation


def _assert_patch_covers_zone() -> None:
    """Refuse to import if a zone setting cannot be patched, or patched wrongly.

    Runs at import rather than only under pytest. The failures this guards
    against — a setting an operator can set on a zone and never change again,
    or one whose patch takes a different type than the zone stores — are silent
    everywhere else, so the process should not start with either.

    Three checks, because there are three ways to drift: a field can be absent,
    it can be present but required (an unset field would then stop meaning
    "untouched"), or it can be present and optional while carrying a type the
    zone will refuse. The last one is why this is not just a name comparison: a
    zone field widened from ``int`` to ``int | str`` and not widened here fails
    only when an operator finally sends the new form.
    """
    patchable = set(Domain.model_fields) - _NOT_PATCHABLE
    missing = sorted(patchable - set(DomainPatch.model_fields))
    if missing:
        raise RuntimeError(
            "DomainPatch is missing Domain fields: "
            + ", ".join(missing)
            + ". Add them as optional, defaulting to None, or an operator can "
            "set them once and never change them."
        )
    required = sorted(
        name for name in patchable if DomainPatch.model_fields[name].default is not None
    )
    if required:
        raise RuntimeError(
            "DomainPatch fields must default to None so an unset field means "
            "'untouched'; these do not: " + ", ".join(required)
        )
    mistyped = sorted(
        f"{name} (zone stores {Domain.model_fields[name].annotation}, patch "
        f"takes {DomainPatch.model_fields[name].annotation})"
        for name in patchable
        if _without_none(DomainPatch.model_fields[name].annotation)
        != _without_none(Domain.model_fields[name].annotation)
    )
    if mistyped:
        raise RuntimeError(
            "every DomainPatch field must accept exactly what the zone stores, "
            "widened only with None; these do not: " + ", ".join(mistyped)
        )


_assert_patch_covers_zone()
