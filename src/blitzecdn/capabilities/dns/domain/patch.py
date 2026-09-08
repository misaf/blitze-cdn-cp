"""A partial update to a zone, and the check that it can express every setting.

``DomainPatch`` inherits ``SitePatch`` rather than restating twenty optional
fields beside it. The fields are the same fields — a zone carries the policy a
site used to — and a second handwritten copy would be a third place to forget
one, which is the failure the parity check below and its counterpart in
:mod:`blitzecdn.capabilities.sites.domain.patch` both exist to make impossible.

The check here is not the one there. That one asks whether every *policy*
setting can be patched; this one asks whether every field of the *zone* can,
which is a larger set by ``origin_host`` and a smaller one by ``name``. A zone
is renamed by deleting it and adding another — the records hang off the name —
so a patch that could set it would be a rename that silently orphaned them.

Like its counterpart, this runs at import rather than only under pytest: a
control plane whose patch and zone have drifted apart should not start.
"""

from __future__ import annotations

from types import UnionType
from typing import Union, get_args, get_origin

from blitzecdn.capabilities.dns.domain.zone import Domain
from blitzecdn.capabilities.sites.domain.patch import SitePatch

__all__ = ["DomainPatch"]

#: Set by adding a zone, changed by deleting one. See the module docstring.
_NOT_PATCHABLE = frozenset({"name"})


class DomainPatch(SitePatch):
    """A partial update to a zone: every field optional, unset means untouched.

    Adds nothing of its own today. It exists as a name rather than an alias so
    that the published schema says what it patches, and so the check below has
    a class to point at when the zone grows a field the patch has not.
    """


def _without_none(annotation: object) -> object:
    """``T`` from ``T | None``, so a patch field and its zone field compare."""
    if get_origin(annotation) is not UnionType and get_origin(annotation) is not Union:
        return annotation
    arms = [arm for arm in get_args(annotation) if arm is not type(None)]
    return arms[0] if len(arms) == 1 else annotation


def _assert_patch_covers_zone() -> None:
    """Refuse to import if a zone setting cannot be patched, or patched wrongly."""
    patchable = set(Domain.model_fields) - _NOT_PATCHABLE
    missing = sorted(patchable - set(DomainPatch.model_fields))
    if missing:
        raise RuntimeError(
            "DomainPatch is missing Domain fields: "
            + ", ".join(missing)
            + ". Add them as optional, defaulting to None, or an operator can "
            "set them once and never change them."
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
