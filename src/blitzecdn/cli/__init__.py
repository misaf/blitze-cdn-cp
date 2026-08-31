"""Command-line adapter package.

``app`` remains re-exported for the supported 3.x introspection surface.  The
actual command assembly lives in :mod:`blitzecdn.cli.main`.
"""

from blitzecdn.cli.main import app

__all__ = ["app"]
