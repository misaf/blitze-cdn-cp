"""Visitor geographical lookup for BlitzeCDN, as a distribution left out.

The control plane ships the settings that ask for a country — the
``BZ-IPCountry`` visitor header and the two firewall country lists — and serves
every site that asks for none of them without this package. Installing it is
what makes those settings deployable: the ``geoip`` capability appears, and the
sites that requested it stop being refused at validation.

Nothing here is imported by the control plane. Discovery is the
``blitzecdn.plugins`` entry point in this distribution's metadata and nothing
else.
"""

from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``PluginMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
