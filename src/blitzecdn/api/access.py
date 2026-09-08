"""Restrict public API requests using the transport peer, never request headers."""

from ipaddress import IPv6Address, ip_address, ip_network

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class AllowedIPsMiddleware:
    def __init__(self, app: ASGIApp, allowed_ips: tuple[str, ...]) -> None:
        self.app = app
        self.networks = tuple(ip_network(value) for value in allowed_ips)

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
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            addresses.append(address.ipv4_mapped)
        # Local health probes and SSH tunnels remain usable after a list change.
        if any(
            candidate.is_loopback
            or any(candidate in network for network in self.networks)
            for candidate in addresses
        ):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse(
                {"detail": "Client IP is not allowed"}, status_code=403
            )
            await response(scope, receive, send)
