"""TLS: the stable contract for how a site is encrypted.

One capability, three parts, and the reason they are one:

* :mod:`~blitzecdn.capabilities.tls.policy` — the configuration contract. The
  encryption mode, the minimum protocol version, the certificate mode and the
  paths BlitzeCDN manages. Pure values; ``sites`` composes them into the flat
  site model without depending on anything below.
* ``blitzecdn-certificates`` — optional issuing, uploading, renewing,
  publishing, and the Automatic SSL/TLS scan that may upgrade ``ssl_mode``.

``automatic_ssl`` was a top-level capability and should not have been: it owns no
model of its own beyond a report, no store, and no setting that is not
``ssl_automatic_mode`` on the TLS policy. It is a mode of this capability —
"may the control plane raise this site's encryption mode for it?" — and reading
it as one removes the second owner of ``SslMode``.

This module re-exports the *contract* and nothing else. Pulling services in here
would make importing ``SslMode`` depend on an optional wheel. Operational code
is discovered from ``blitzecdn-certificates`` through ``blitzecdn.plugins``.
"""

from blitzecdn.capabilities.tls.policy import (
    CERTIFICATE_ROOTS,
    MANAGED_TLS_ROOT,
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
    managed_certificate_paths,
)

__all__ = [
    "CERTIFICATE_ROOTS",
    "MANAGED_TLS_ROOT",
    "CertificateMode",
    "MinimumTlsVersion",
    "SslAutomaticMode",
    "SslMode",
    "TlsPolicy",
    "managed_certificate_paths",
]
