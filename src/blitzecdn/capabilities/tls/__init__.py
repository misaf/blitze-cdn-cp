"""TLS policy and certificate management.

Contracts across the fleet name `tls.contract`, and importing it runs this
module first. `dns.domain` is one of them, and TLS's implementation depends on
dns, so this module imports nothing eagerly beyond the contract: the ports
other capabilities may use load on first access instead.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from blitzecdn.capabilities.tls.ports import CertificateHistory

#: Public names loaded on first access, by the module that defines them.
_LAZY = {"CertificateHistory": "blitzecdn.capabilities.tls.ports"}


def __getattr__(name: str) -> Any:
    try:
        module = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "CERTIFICATE_ROOTS",
    "MANAGED_TLS_ROOT",
    "CertificateHistory",
    "CertificateMode",
    "CertificateVersion",
    "MinimumTlsVersion",
    "SslAutomaticMode",
    "SslMode",
    "TlsPolicy",
    "managed_certificate_paths",
]
