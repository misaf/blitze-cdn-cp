from blitzecdn_compression.plugin import blitzecdn_plugin_metadata

from blitzecdn.core.plugins import PluginMetadata
from blitzecdn.features.compression.policy import CompressionMode, CompressionPolicy


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


def test_plugin_provides_compression() -> None:
    metadata = blitzecdn_plugin_metadata()
    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "compression"
    assert metadata.capabilities == frozenset({"compression"})
    assert not metadata.required
