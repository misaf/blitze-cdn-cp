"""HTTP/3 over QUIC for BlitzeCDN, as a distribution that can be left out.

The control plane ships the ``http3_enabled`` switch and serves HTTP/1.1 and
HTTP/2 without this package. Installing it is what makes the switch mean
something: the fleet opens a QUIC listener, and exactly one server block is
named to carry ``reuseport``.

Nothing here is imported by the control plane. Discovery is the
``blitzecdn.plugins`` entry point in this distribution's metadata and nothing
else.
"""

from blitzecdn.core.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
