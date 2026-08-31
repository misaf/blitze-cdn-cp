"""The scheduled operations, and the order they imply.

A vertical slice like any other, and here rather than in ``core`` because it
depends on three features. ``core`` is what a feature may build on; a module
there that reaches back into certificates, automatic SSL and deployments points
the arrow the wrong way and hides a genuine cross-feature workflow inside the
foundation package.
"""

from blitzecdn.features.maintenance.service import MaintenanceService

__all__ = ["MaintenanceService"]
