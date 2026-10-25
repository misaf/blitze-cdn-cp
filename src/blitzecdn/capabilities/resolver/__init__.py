"""The resolver capability: edge-host roles and nothing else."""

from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

__all__ = ["__version__"]
