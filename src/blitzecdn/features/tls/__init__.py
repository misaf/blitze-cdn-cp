"""TLS: how a site is encrypted, and everything that keeps it that way.

One capability, three parts, and the reason they are one:

* :mod:`~blitzecdn.features.tls.policy` — the configuration contract. The
  encryption mode, the minimum protocol version, the certificate mode and the
  paths BlitzeCDN manages. Pure values; ``sites`` composes them into the flat
  site model without depending on anything below.
* :mod:`~blitzecdn.features.tls.certificates` — issuing, uploading, renewing,
  and publishing the material those paths point at.
* :mod:`~blitzecdn.features.tls.automatic_ssl` — the Cloudflare-style scan that
  *upgrades* ``ssl_mode`` once the edges confirm an origin answers on TLS.

``automatic_ssl`` was a top-level feature and should not have been: it owns no
model of its own beyond a report, no store, and no setting that is not
``ssl_automatic_mode`` on the TLS policy. It is a mode of this capability —
"may the control plane raise this site's encryption mode for it?" — and reading
it as one removes the second owner of ``SslMode``.

This module re-exports the *contract* and nothing else. Pulling the services in
here would make importing ``SslMode`` execute the certificate stack, and
``sites`` imports ``SslMode``; the implementations are reached through
``tls.certificates`` and ``tls.automatic_ssl``, which is where they live.
"""

from blitzecdn.features.tls.policy import (
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
