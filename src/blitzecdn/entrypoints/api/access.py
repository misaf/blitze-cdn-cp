"""Restrict public API requests using the transport peer, never request headers."""

import logging
from ipaddress import IPv6Address, ip_address, ip_network

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOGGER = logging.getLogger(__name__)

# Headers a reverse proxy adds. They never decide access; they only reveal that
# a loopback peer is relaying someone else.
_FORWARDING_HEADERS = frozenset({b"x-forwarded-for", b"forwarded"})


class AllowedIPsMiddleware:
    def __init__(self, app: ASGIApp, allowed_ips: tuple[str, ...]) -> None:
        self.app = app
        self.networks = tuple(ip_network(value) for value in allowed_ips)
        self.proxy_warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        try:
            address = ip_address(peer[0]) if peer else None
        except ValueError:
            address = None
        addresses = [address] if address is not None else []
        # A dual-stack socket reports IPv4 clients as ::ffff:198.51.100.8, so both
        # forms match; genuine IPv6 matches nothing.
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            addresses.append(address.ipv4_mapped)
        # Local health probes and SSH tunnels remain usable after a list change.
        loopback = any(candidate.is_loopback for candidate in addresses)
        if loopback:
            self._warn_if_proxied(scope)
        if loopback or any(
            candidate in network for candidate in addresses for network in self.networks
        ):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse(
                {"detail": "Client IP is not allowed"}, status_code=403
            )
            await response(scope, receive, send)

    def _warn_if_proxied(self, scope: Scope) -> None:
        """Warn once when a same-host proxy turns every client into loopback."""
        if self.proxy_warned:
            return
        if any(name in _FORWARDING_HEADERS for name, _ in scope.get("headers", ())):
            self.proxy_warned = True
            _LOGGER.warning(
                "A loopback request carried a forwarding header: a reverse proxy "
                "on this host is relaying clients, and loopback is always "
                "allowed, so allowed_ips no longer filters them. Restrict the "
                "proxy itself or remove it."
            )
