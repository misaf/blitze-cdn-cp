"""The cache capability's contract.

Contract only. Purging a cached response and reading how well the cache is
working are *operations*, and they ship in the detachable ``blitzecdn-cache``
distribution because a control plane without them is a control plane that
merely cannot purge. What lives here is how a site asks to be cached, which
has to load whether or not that distribution is installed: a stored site with
``cache_enabled`` set must still read back on a core-only controller, and the
deployment is refused by name through
:attr:`~blitzecdn.capabilities.cache.policy.CachePolicy.capability_requirements`
rather than by failing to parse.

The same split as ``compression``, ``http`` and ``tls``, for the same reason.
"""
