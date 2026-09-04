"""The flat site document the edge roles read.

This is a projection of `sites`' own model onto somebody else's vocabulary,
which is what an adapter is. It sat in ``core.ansible.mapping`` beside
``edge_to_inventory``, one module holding two capabilities' projections, and
core imported `CdnSite` to do it — the foundation reaching up into the tree it
supports for a document only `sites` has ever produced or consumed.
"""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty

__all__ = ["site_to_ansible"]


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """The flat site document, minus every block that declares itself absent.

    The pruning is by declaration rather than by name. This used to read ``if
    site.firewall.empty``, which is a capability's own vocabulary in a generic
    adapter: core knew what a firewall was, and a second such block would have
    been a second branch here. A block opts in by subclassing
    :class:`~blitzecdn.core.domain.validation.OmittedWhenEmpty`, and this asks
    nothing about what it holds.
    """
    document = site.model_dump(mode="json", exclude_none=True)
    for field in type(site).model_fields:
        value = getattr(site, field)
        if isinstance(value, OmittedWhenEmpty) and value.empty:
            document.pop(field, None)
    return document
