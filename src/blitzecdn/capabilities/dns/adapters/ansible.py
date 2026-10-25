"""Serialize derived virtual hosts into the flat document consumed by edge roles."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty

__all__ = ["site_to_ansible"]


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """Serialize a host, omitting nulls and empty ``OmittedWhenEmpty`` blocks."""
    document = serving_document(site)
    document = {name: value for name, value in document.items() if value is not None}
    for field, value in site.policy.settings().items():
        if isinstance(value, OmittedWhenEmpty) and value.empty:
            document.pop(field, None)
    return document
