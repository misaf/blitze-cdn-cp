"""Origin request identity policy for a site."""

from pydantic import BaseModel, ConfigDict, field_validator

from blitzecdn.core.validation import hostname


class OriginPolicy(BaseModel):
    """Host header and SNI overrides used on the origin leg."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_request_host: str | None = None
    origin_sni: str | None = None

    @field_validator("origin_request_host", "origin_sni")
    @classmethod
    def validate_optional_host(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None
