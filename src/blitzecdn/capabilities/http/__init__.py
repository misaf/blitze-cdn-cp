"""Visitor-facing protocol versions, schemes and listener contracts.

HTTP/1.1 and HTTP/2 are baseline; HTTP/3 is opt-in policy implemented by the
nested `http3` slice.
"""

from blitzecdn.capabilities.http.contract import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
    ProtocolPolicy,
)

__all__ = [
    "DEFAULT_PORTS",
    "HTTPS_PROXY_PORTS",
    "HTTP_PROXY_PORTS",
    "HttpScheme",
    "ProtocolPolicy",
]
