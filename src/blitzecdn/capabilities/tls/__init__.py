"""TLS policy, certificate workflows and automatic origin upgrades.

Shipped with the application. Contracts stay pure; services and concrete
providers are assembled explicitly by composition.
"""

from blitzecdn.capabilities.tls.contract import (
    CERTIFICATE_ROOTS,
    MANAGED_TLS_ROOT,
    CertificateMode,
    CertificateVersion,
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
    "CertificateVersion",
    "MinimumTlsVersion",
    "SslAutomaticMode",
    "SslMode",
    "TlsPolicy",
    "managed_certificate_paths",
]
