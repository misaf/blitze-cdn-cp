"""The HTTP capability's configuration contract.

The scheme, the public proxy port sets, and the one protocol switch a site
owns. ``sites`` composes :class:`ProtocolPolicy` into its flat policy.

The contract is deliberately wider than what this distribution implements.
HTTP/1.1 and HTTP/2 are invariants of the managed edge and carry no policy;
HTTP/3 is a switch a site opts into, and the code that turns it into a QUIC
listener ships separately as ``blitzecdn-http3``. The *field* stays here so
that a stored site asking for HTTP/3 still loads when that distribution is
absent — the control plane then refuses the deployment by name through
:attr:`required_capabilities`, rather than failing to read its own database or
quietly serving the site over HTTP/2 as though nothing had been asked for.

The request body limit sits here for the opposite reason. It is a property of
how the edge reads an HTTP request rather than of anything detachable: core
nginx enforces it with a directive that is always compiled in, so the field
asks for no capability token and a site carrying it converges on an edge with
nothing installed beside the control plane.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict

from blitzecdn.core.domain.policy import CapabilityPolicy


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


# Cloudflare-compatible public proxy ports. These mirror the edge runtime
# listener contract and are intentionally independent sets rather than pairs.
HTTP_PROXY_PORTS = (80, 8080, 8880, 2052, 2082, 2086, 2095)
HTTPS_PROXY_PORTS = (443, 2053, 2083, 2087, 2096, 8443)
DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}


class MaxUploadSize(StrEnum):
    """Largest visitor request body one site accepts.

    Members are nginx sizes, rendered straight into ``client_max_body_size``. A
    closed set rather than a free size string on purpose: the tiers are the
    product decision, and carrying them in the type means the API, the CLI and
    the role's ``choices`` all reject an unknown value without any of them
    growing a validator of its own.

    nginx's own compiled-in default is 1m, which is not one of these. A site
    that has never been configured therefore accepts more than it used to, not
    less, so nothing that succeeds against an unmanaged edge starts failing.
    """

    SMALL = "100m"
    LARGE = "200m"


class ProtocolPolicy(CapabilityPolicy):
    """HTTP protocol behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # QUIC always negotiates TLS 1.3. This does not change the minimum TLS
    # version accepted by the site's parallel TCP listeners.
    http3_enabled: bool = False

    max_upload_size: MaxUploadSize = MaxUploadSize.SMALL

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Implementation capabilities requested by this stable policy.

        Empty for a site served over HTTP/1.1 and HTTP/2, which every managed
        edge does with nothing installed beside the control plane.
        """
        if not self.http3_enabled:
            return {}
        return {"http3": ("http3_enabled",)}
