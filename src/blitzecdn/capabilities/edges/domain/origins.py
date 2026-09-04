"""One edge's answer about one site's origin.

Only the row. The *fleet report* — every edge's answer, read per site — left
with the play that produces it, into ``blitzecdn-origins``. This shape stayed
because it has a second producer that runs no play at all: the controller's own
advisory probe inside certificate preflight, which has to answer in
milliseconds during issuance. A row type that travelled with the wheel would
take that probe's return type away from a controller that had detached it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.capabilities.tls.policy import SslMode


class OriginCheck(BaseModel):
    """One edge's answer about one site's origin.

    Two separate answers, because the fixes differ. ``reachable`` false means
    DNS, routing, a firewall, or the wrong port — the edge got nothing back at
    all. ``tls_verified`` false with ``reachable`` true means the origin
    answered but presented a certificate that is not valid for the SNI this
    edge sends, which is the site's ``origin_sni`` or the origin's certificate,
    and nothing to do with the network. ``tls_verified`` is ``None`` for an HTTP
    origin or Full mode, where verification is deliberately not attempted.

    ``status`` is whatever the origin answered a ``HEAD /`` with, and is not
    judged: a 404 or a 403 still proves the origin is up and talking, which is
    the whole of what this check claims to know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    origin: str
    scheme: HttpScheme
    ssl_mode: SslMode
    sni: str | None = None
    reachable: bool = False
    tls_verified: bool | None = None
    #: The HTTP status the origin answered with, or ``None`` if it did not.
    status: int | None = None
    #: Present only for Automatic SSL/TLS scans, which compare the response
    #: reached over the current transport with the same response over HTTPS.
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tls_verified is not False
