"""The upgrade-only Automatic SSL/TLS scan, a mode of certificate management."""

from blitzecdn.capabilities.tls.automatic_ssl.domain import SslAutomaticReconciliation
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService

__all__ = ["AutomaticSslService", "SslAutomaticReconciliation"]
