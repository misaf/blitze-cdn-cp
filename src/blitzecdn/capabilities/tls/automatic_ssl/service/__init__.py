"""Recommending an encryption mode, and raising a site to it.

`upgrades.py` reads the fleet's origin scan, decides what each enrolled site
could safely serve, and moves the ones that may be moved.
"""

from blitzecdn.capabilities.tls.automatic_ssl.service.upgrades import (
    AutomaticSslService,
)

__all__ = ["AutomaticSslService"]
