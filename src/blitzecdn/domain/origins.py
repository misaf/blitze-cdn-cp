"""What the controller learns by connecting to a site's origin itself."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from blitzecdn.domain.sites import HttpScheme


class OriginCheck(BaseModel):
    """The result of connecting to one site's origin the way the edge will.

    Three separate answers, because the fixes differ. ``resolved`` false means
    DNS; ``reachable`` false means routing, a firewall, or the wrong port;
    ``tls_verified`` false means the origin's certificate does not match the
    SNI the edge will send. ``tls_verified`` is ``None`` for an HTTP origin,
    where the question does not arise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    origin: str
    scheme: HttpScheme
    sni: str | None = None
    resolved: bool = False
    reachable: bool = False
    tls_verified: bool | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tls_verified is not False
