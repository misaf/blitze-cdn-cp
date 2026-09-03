"""The site-serving contract: one virtual host and the policy attached to it.

Deliberately narrow. ``sites`` owns the *composition* — ``CdnSite``,
``SitePolicy``, and the cross-capability rules that only make sense once every
fragment is on one model — plus the three fragments nothing else owns, and now
the service that writes them: a site is canonical rather than derived. It does
not re-export compression, HTTP, security or TLS values: importing
``CompressionMode`` from here would put a second name on a contract that has an
owner, and the ownership tests refuse exactly that.
"""

from blitzecdn.features.sites.domain import CdnSite, SitePatch, SitePolicy
from blitzecdn.features.sites.policy import (
    CachePolicy,
    CacheQueryStringMode,
    HeaderPolicy,
    OriginPolicy,
    SiteVisitorHeaders,
)
from blitzecdn.features.sites.service import SiteService

__all__ = [
    "CachePolicy",
    "CacheQueryStringMode",
    "CdnSite",
    "HeaderPolicy",
    "OriginPolicy",
    "SitePatch",
    "SitePolicy",
    "SiteService",
    "SiteVisitorHeaders",
]
