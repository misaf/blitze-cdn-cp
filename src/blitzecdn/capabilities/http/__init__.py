"""Visitor-facing protocol versions, schemes and listener contracts.

HTTP/1.1 and HTTP/2 are baseline edge protocols. HTTP/3 is an opt-in protocol
policy, implemented by the nested `http3` slice, which declares its own feature.
The application always ships both the contract and the slice; policy controls whether
QUIC listeners and their runtime requirements are emitted."""

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
