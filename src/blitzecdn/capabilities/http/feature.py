"""Declare the baseline HTTP capability and the fleet's listener stance.

The two QUIC variables are contributed at their baseline — no listener, no
owner — because the edge roles require them; the ``http3`` feature overrides
both. A site asking for HTTP/3 without the capability is refused at validation.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.http import cli
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="http",
            version=__version__,
            summary="Baseline visitor HTTP/1.1 and HTTP/2, and the listener contract.",
        ),
        cli_commands=(
            CliCommandGroup(
                feature="http",
                name="domain",
                app=cli.domain_commands,
                extends=True,
            ),
        ),
    ),
)
