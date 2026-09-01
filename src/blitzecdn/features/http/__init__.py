"""Visitor-facing HTTP: protocol versions, schemes, and the listener contract.

HTTP/1.1, HTTP/2 and HTTP/3 are versions of one protocol served on one set of
listeners, so one capability owns the contract for all three. HTTP/1.1 and
HTTP/2 are invariants of the managed edge and carry no policy; HTTP/3 is the
one a site opts into, which makes it a switch on this capability rather than a
capability of its own — ``features/http3`` was a package once and the layering
tests still refuse the name.

What *is* separable is the implementation behind that switch. Turning enabled
sites into a QUIC listener and choosing the one server block that carries
``reuseport`` ships as ``blitzecdn-http3``, an optional distribution found only
through its entry point. The switch stays here with the ports and schemes it
constrains, so the site contract is identical whether or not that distribution
is installed; only the fleet's listener state changes.
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
