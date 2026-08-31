"""The site's own configuration fragments.

What is left here is what belongs to *sites* and to nothing else: how a site
caches, which trusted ``BZ-*`` headers it writes, and how it identifies itself
to its origin. Each is a value type with no behaviour anywhere but on the site.

Cache is the one that looks like it should have moved. There is a ``cache``
capability, but it owns *operations* — purge and statistics — and its service
consumes ``CdnSite`` to carry them out. Moving ``CachePolicy`` under it would
make ``sites`` depend on the feature that already depends on ``sites``, which
buys nothing: nobody but a site's own configuration reads these four fields.
Compression, HTTP, security and TLS moved because their behaviour genuinely
lives elsewhere; these three have nowhere else to be.
"""

from blitzecdn.features.sites.policy.cache import CachePolicy, CacheQueryStringMode
from blitzecdn.features.sites.policy.headers import HeaderPolicy, SiteVisitorHeaders
from blitzecdn.features.sites.policy.origin import OriginPolicy

__all__ = [
    "CachePolicy",
    "CacheQueryStringMode",
    "HeaderPolicy",
    "OriginPolicy",
    "SiteVisitorHeaders",
]
