from blitzecdn.capabilities.compression.contract import (
    CompressionMode,
    CompressionPolicy,
)
from blitzecdn.capabilities.compression.feature import FEATURES
from blitzecdn.core.features import FeatureMetadata

(FEATURE,) = FEATURES


def test_gzip_and_brotli_are_strategies_of_one_capability() -> None:
    assert set(CompressionMode) == {
        CompressionMode.OFF,
        CompressionMode.GZIP,
        CompressionMode.BROTLI,
    }


def test_only_enabled_compression_requires_the_implementation() -> None:
    disabled = CompressionPolicy(compression=CompressionMode.OFF)
    enabled = CompressionPolicy(compression=CompressionMode.GZIP)

    assert disabled.required_capabilities == frozenset()
    assert enabled.required_capabilities == frozenset({"compression"})


def test_feature_provides_compression() -> None:
    metadata = FEATURE.metadata
    assert isinstance(metadata, FeatureMetadata)
    assert metadata.name == "compression"
    assert metadata.capabilities == frozenset({"compression"})
