"""Partial zone policy updates, also used to validate rule overrides.

Unset fields are untouched; the merged zone is validated as a whole. Drift
between the zone and this model is refused at import.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.cache.contract import CacheQueryStringMode
from blitzecdn.capabilities.compression.contract import CompressionMode
from blitzecdn.capabilities.http.contract import MaxUploadSize
from blitzecdn.capabilities.security.contract import SiteFirewall
from blitzecdn.capabilities.serving.contract import SitePolicy, SiteVisitorHeaders
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)

__all__ = ["DomainPatch", "reject_issuer_owned_certificate"]


def reject_issuer_owned_certificate(changes: Mapping[str, Any]) -> None:
    """Refuse an operator edit that claims controller-managed TLS material.

    Called only from the operator authoring paths. The issuer writes a rule's
    certificate as an overrides mapping carrying ``requested`` through
    ``replace_rule``, so the check is about who sets the value, which only the
    entry point knows. Without it the zone's hostnames go silently dark until
    ``blitzecdn validate`` names them.
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

    Fields are written out, not generated with ``create_model``, so mypy checks
    every access; ``_assert_patch_covers_settings`` refuses drift at import.
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
    # Replaces the block wholesale; see the note on SecurityPolicy.firewall. Send
    # {"firewall": {}} to clear every rule.
    firewall: SiteFirewall | None = None
    # Replaced wholesale as well. Sending {"visitor_headers": {}} restores the
    # defaults rather than leaving the current switches in place.
    visitor_headers: SiteVisitorHeaders | None = None


def _without_none(annotation: object) -> object:
    """``T`` from ``T | None`` on both sides, so optional zone fields compare."""
    if get_origin(annotation) is not UnionType and get_origin(annotation) is not Union:
        return annotation
    arms = [arm for arm in get_args(annotation) if arm is not type(None)]
    return arms[0] if len(arms) == 1 else annotation


def _assert_patch_covers_settings() -> None:
    """Refuse to import if a zone setting cannot be patched, or patched wrongly.

    Checks each setting is present, optional, and of a type the zone accepts —
    failures that would otherwise appear only when an operator first tries.
    """
    patchable = set(SitePolicy.setting_fields())
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
        f"{name} (zone stores {SitePolicy.setting_fields()[name].annotation}, patch "
        f"takes {DomainPatch.model_fields[name].annotation})"
        for name in patchable
        if _without_none(DomainPatch.model_fields[name].annotation)
        != _without_none(SitePolicy.setting_fields()[name].annotation)
    )
    if mistyped:
        raise RuntimeError(
            "every DomainPatch field must accept exactly what the zone stores, "
            "widened only with None; these do not: " + ", ".join(mistyped)
        )


_assert_patch_covers_settings()
