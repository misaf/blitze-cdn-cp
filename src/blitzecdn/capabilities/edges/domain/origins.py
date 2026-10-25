"""One edge's answer about one site's origin.

The row only; the fleet report is ``origins``'. It lives here because
certificate preflight's controller probe produces it too.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.capabilities.tls.contract import SslMode


class OriginCheck(BaseModel):
    """One edge's answer about one site's origin.

    ``reachable`` false is a network problem; ``tls_verified`` false with
    ``reachable`` true is a certificate mismatch for the SNI sent; ``None`` when
    verification is not attempted. ``status`` is unjudged: any answer means the
    origin is up.
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
