"""A partial update to a site, and the check that it can express every setting.

Separate from :mod:`~blitzecdn.capabilities.sites.domain.site` because it is a
different kind of value with a different lifetime: `CdnSite` is what a site
*is*, `SitePatch` is one request to change one, and the two are related only by
the parity this module asserts. Splitting them puts that assertion beside the
model it constrains rather than under three hundred lines of the model it
compares against.

``_assert_patch_covers_policy`` still runs at import rather than only under
pytest, and importing :mod:`blitzecdn.capabilities.sites.domain` imports this,
so a control plane whose patch and policy have drifted apart still refuses to
start.
"""

from __future__ import annotations

from types import UnionType
from typing import Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.capabilities.sites.domain.site import SitePolicy
from blitzecdn.capabilities.sites.policy import SiteVisitorHeaders
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)

__all__ = ["SitePatch"]


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
