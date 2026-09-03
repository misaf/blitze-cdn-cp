"""The site's own configuration fragments.

What is left here is what belongs to *sites* and to nothing else: which trusted
``BZ-*`` headers a site writes, and how it identifies itself to its origin.
Both are value types with no behaviour anywhere but on the site, and — this is
the test — there is no distribution that could carry either away.

``CachePolicy`` was here and is not any more. The argument for keeping it was
that the ``cache`` capability owns *operations* — purge and statistics — whose
service consumes ``CdnSite``, so moving the contract under it would point
``sites`` at the capability that already depends on ``sites``. That is a cycle
between *implementations*, which is not what a contract edge is: a capability's
``policy`` module imports nothing but ``core``, so ``cache/policy.py`` can be
composed here exactly as ``compression`` and ``tls`` are. Keeping it was
``sites`` owning a setting the ``cache`` wheel exists to serve.
"""

from blitzecdn.capabilities.sites.policy.headers import HeaderPolicy, SiteVisitorHeaders
from blitzecdn.capabilities.sites.policy.origin import OriginPolicy

__all__ = [
    "HeaderPolicy",
    "OriginPolicy",
    "SiteVisitorHeaders",
]
