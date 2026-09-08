"""The configuration fragments that belong to a served hostname and nowhere else.

What is here is what no capability could carry away: which trusted ``BZ-*``
headers are written on the request to the origin, and how the edge identifies
itself to that origin. Both are value types with no behaviour outside the
policy a zone holds, and — this is the test — there is no distribution that
could take either.

They sit beside the zone because the zone holds the policy they belong to.
Under `sites` they would be left behind with a capability whose only remaining
job is to own two value types.
"""

from blitzecdn.capabilities.dns.policy.headers import HeaderPolicy, SiteVisitorHeaders
from blitzecdn.capabilities.dns.policy.origin import OriginPolicy

__all__ = [
    "HeaderPolicy",
    "OriginPolicy",
    "SiteVisitorHeaders",
]
