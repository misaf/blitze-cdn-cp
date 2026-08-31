"""Visitor-facing HTTP: protocol versions, schemes, and the listener contract.

HTTP/1.1, HTTP/2 and HTTP/3 are versions of one protocol served on one set of
listeners, so they belong to one capability. HTTP/1.1 and HTTP/2 are invariants
of the managed edge and carry no policy; HTTP/3 is the one a site opts into,
which makes it a switch on this capability rather than a feature — it was a
top-level ``http3`` package once, and reuniting it with the ports and schemes it
constrains is what let the QUIC fleet contribution live beside them.
"""

from blitzecdn.features.http.policy import (
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
