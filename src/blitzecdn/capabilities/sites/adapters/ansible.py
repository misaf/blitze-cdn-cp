"""Serialize derived virtual hosts into the flat document consumed by edge roles."""

from __future__ import annotations

import hashlib
from typing import Any

from blitzecdn.capabilities.sites.adapters.documents import serving_document
from blitzecdn.core.domain.paths import path_pattern
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty

__all__ = ["site_to_ansible", "variable_scope"]


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """Serialize a host, omitting nulls and empty ``OmittedWhenEmpty`` blocks.

    ``locations`` is left out when a host has none, so a host without path rules
    is handed exactly what it was before they existed. Each location carries its
    ``variable_scope``: the suffix of the http-level variables its path-scoped
    settings are read through. It holds an underscore, which no host name can,
    so it never names a host's own variables. It also carries its ``pattern``,
    the anchored regex :func:`~blitzecdn.core.domain.paths.path_pattern` builds,
    which the template renders as given rather than spelling the rule again.
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
            {
                **location,
                "pattern": path_pattern(location["path"]),
                "variable_scope": variable_scope(site.name, location["rule"]),
            }
            for location in document["locations"]
        ]
    return document


def variable_scope(host: str, rule: str) -> str:
    """A fixed-length name for one host's path rule in Nginx variable names.

    Bounded, because a host name and a rule name together can exceed what
    ``variables_hash_bucket_size`` holds, and Nginx then refuses the whole
    configuration. Keyed by a digest of both, so distinct pairs stay distinct.
    """
    # A name, not a security use; also keeps FIPS builds working.
    digest = hashlib.sha256(f"{host}/{rule}".encode(), usedforsecurity=False)
    return f"path_{digest.hexdigest()[:16]}"
