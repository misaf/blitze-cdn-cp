"""The compression capability's configuration contract."""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict

from blitzecdn.core.domain.policy import CapabilityPolicy


class CompressionMode(StrEnum):
    """Which encodings a managed edge may produce.

    ``brotli`` includes gzip fallback for clients that do not advertise ``br``.
    Origin-encoded responses remain pass-through under every mode.
    """

    OFF = "off"
    GZIP = "gzip"
    BROTLI = "brotli"


class CompressionPolicy(CapabilityPolicy):
    """Compression behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compression: CompressionMode = CompressionMode.BROTLI

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Implementation capabilities requested by this stable policy."""
        if self.compression is CompressionMode.OFF:
            return {}
        return {"compression": ("compression",)}
