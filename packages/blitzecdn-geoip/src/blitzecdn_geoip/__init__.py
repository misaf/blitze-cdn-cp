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

__version__ = "3.0.0"

__all__ = ["__version__"]
