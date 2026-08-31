"""The compression capability: one switch, two strategies, one owner."""

import pytest
from pydantic import ValidationError

from blitzecdn.features.compression import plugin
from blitzecdn.features.compression.policy import CompressionMode, CompressionPolicy


def test_gzip_and_brotli_are_strategies_of_one_capability():
    """Not two features. They share the switch, and neither has a lifecycle.

    `brotli` implies gzip for clients that do not advertise `br`, which is the
    clearest statement that these are two encodings of one decision rather than
    two capabilities that happen to sit near each other.
    """
    assert [mode.value for mode in CompressionMode] == ["off", "gzip", "brotli"]
    assert CompressionPolicy().compression is CompressionMode.BROTLI


def test_compression_policy_is_frozen_and_refuses_an_unknown_encoding():
    policy = CompressionPolicy(compression=CompressionMode.GZIP)

    with pytest.raises(ValidationError):
        CompressionPolicy(compression="zstd")
    with pytest.raises(ValidationError):
        policy.compression = CompressionMode.OFF
    with pytest.raises(ValidationError):
        CompressionPolicy(gzip_level=9)


def test_the_capability_registers_itself_and_contributes_nothing_else():
    """A capability contributes what it has, not a shape it has to fill."""
    metadata = plugin.blitzecdn_plugin_metadata()

    assert metadata.name == "compression"
    assert metadata.required
    hooks = [name for name in vars(plugin) if name.startswith("blitzecdn_")]
    assert hooks == ["blitzecdn_plugin_metadata"]
