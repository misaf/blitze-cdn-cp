"""What one Automatic SSL/TLS pass concluded.

`reconciliation.py` is that result: which sites were scanned, and which had
their encryption mode raised as a consequence.
"""

from blitzecdn_certificates.automatic_ssl.domain.reconciliation import (
    SslAutomaticReconciliation,
)

__all__ = ["SslAutomaticReconciliation"]
