"""Declare the required maintenance job executor capability."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.features import Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="maintenance",
            version=__version__,
            summary="Execute and converge scheduled capability jobs.",
        ),
    ),
)
