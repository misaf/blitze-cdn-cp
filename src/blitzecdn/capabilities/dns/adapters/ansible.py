"""Serialize derived virtual hosts into the flat document consumed by edge roles."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty

__all__ = ["site_to_ansible"]


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """Serialize a host, omitting nulls and empty ``OmittedWhenEmpty`` blocks.

    ``locations`` is left out when a host has none, so a host without path rules
    is handed exactly what it was before they existed. Each location carries its
    ``variable_scope``: the suffix of the http-level variables its path-scoped
    settings are read through. It holds an underscore, which no host name can,
    so it never names a host's own variables.
    """
    document = serving_document(site)
    document = {name: value for name, value in document.items() if value is not None}
    for field, value in site.policy.settings().items():
        if isinstance(value, OmittedWhenEmpty) and value.empty:
            document.pop(field, None)
    if not site.locations:
        document.pop("locations", None)
    else:
        document["locations"] = [
            {**location, "variable_scope": f"{site.name}_path_{location['rule']}"}
            for location in document["locations"]
        ]
    return document
