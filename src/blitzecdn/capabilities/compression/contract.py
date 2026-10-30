"""The compression capability's configuration contract."""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field

from blitzecdn.core.domain.policy import CapabilityPolicy


class CompressionMode(StrEnum):
    """Which encodings the edge may produce; origin-encoded bodies pass through."""

    OFF = "off"
    GZIP = "gzip"
    BROTLI = "brotli"


class CompressionPolicy(CapabilityPolicy):
    """Compression behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compression: CompressionMode = Field(
        default=CompressionMode.BROTLI,
        description=(
            "Encodings this edge produces itself. brotli offers Brotli to clients "
            "that accept it and gzip to the rest; off stops the edge compressing. "
            "Origin-encoded bodies pass through every mode."
        ),
    )

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Implementation capabilities requested by this stable policy."""
        if self.compression is CompressionMode.OFF:
            return {}
        return {"compression": ("compression",)}
