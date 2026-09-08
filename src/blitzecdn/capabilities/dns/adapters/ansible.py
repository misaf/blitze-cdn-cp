"""Serialize derived virtual hosts into the flat document consumed by edge roles."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty

__all__ = ["site_to_ansible"]


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """Serialize a host, omitting nulls and empty blocks that opt into omission.

    Blocks declare this behavior with ``OmittedWhenEmpty``; the adapter does not
    need to know their capability-specific fields."""
    document = site.model_dump(mode="json", exclude_none=True)
    for field in type(site).model_fields:
        value = getattr(site, field)
        if isinstance(value, OmittedWhenEmpty) and value.empty:
            document.pop(field, None)
    return document
