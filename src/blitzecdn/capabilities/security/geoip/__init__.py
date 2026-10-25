"""The geoip slice of the security capability: edge roles and Nginx fragments.

This implementation and its edge resources ship in the BlitzeCDN distribution.
Configuration controls their use; the installed application always includes them.
"""

from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``FeatureMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
