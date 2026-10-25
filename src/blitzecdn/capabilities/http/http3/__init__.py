"""The http3 slice of the http capability: edge roles and Nginx fragments."""

from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
