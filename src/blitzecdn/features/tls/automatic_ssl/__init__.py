"""The upgrade-only Automatic SSL/TLS scan, a mode of the TLS capability."""

from blitzecdn.features.tls.automatic_ssl.domain import SslAutomaticReconciliation
from blitzecdn.features.tls.automatic_ssl.service import AutomaticSslService

__all__ = ["AutomaticSslService", "SslAutomaticReconciliation"]
