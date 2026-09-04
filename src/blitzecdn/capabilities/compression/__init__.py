"""Response compression: the capability's contract, and its strategies.

Contract only. Compressing a response is work the edge does, and the nginx
configuration and the role that do it ship in the detachable
``blitzecdn-compression`` distribution. What lives here is how a site asks to
be compressed, which has to load whether or not that distribution is
installed: a stored site with ``compression_enabled`` set must still read back
on a core-only controller, and the deployment is refused by name through
:attr:`~blitzecdn.core.domain.policy.CapabilityPolicy.capability_requirements`
rather than by failing to parse.

gzip and Brotli are *strategies* of this one capability, not capabilities of
their own — they answer the same question (may the edge compress this response,
and with what), share one on/off switch, one minimum length and one MIME type
list, and have no lifecycle either could have without the other. So they are
values of :class:`CompressionMode` here rather than two capability packages, and
``tests/architecture/test_layering.py`` refuses a top-level ``gzip`` or
``brotli`` package by name.

The same split as ``cache``, ``http``, ``security`` and ``tls``, for the same
reason.
"""

from blitzecdn.capabilities.compression.policy import CompressionMode, CompressionPolicy

__all__ = ["CompressionMode", "CompressionPolicy"]
