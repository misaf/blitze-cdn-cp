"""Automatic SSL/TLS: scan origins and raise each site to its strongest safe mode.

A workflow on top of TLS, not part of it: TLS owns certificates and the
encryption policy; this capability reads sites, probes origins from every
edge, writes the upgrade back through TLS and deploys the result. No other
capability builds on it, so its public surface is its version alone.
"""

from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
