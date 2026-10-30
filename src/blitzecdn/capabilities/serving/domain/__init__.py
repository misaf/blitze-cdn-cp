"""The effective virtual host consumed across application modules."""

from blitzecdn.capabilities.serving.domain.edits import EditVisitorHeaders
from blitzecdn.capabilities.serving.domain.host import CdnSite

__all__ = ["CdnSite", "EditVisitorHeaders"]
