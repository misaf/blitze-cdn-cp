"""The compression capability's configuration contract."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CompressionMode(StrEnum):
    """Which encodings a managed edge may produce.

    ``brotli`` includes gzip fallback for clients that do not advertise ``br``.
    Origin-encoded responses remain pass-through under every mode.
    """

    OFF = "off"
    GZIP = "gzip"
    BROTLI = "brotli"


class CompressionPolicy(BaseModel):
    """Compression behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compression: CompressionMode = CompressionMode.BROTLI
