"""Under Attack Mode's typed controller configuration."""

from typing import Final

from pydantic import Field, SecretStr, field_validator

from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "MINIMUM_SECRET_BYTES", "SECRET_VARIABLE", "SecurityConfig"]

SECRET_VARIABLE: Final = "BLITZE_UNDER_ATTACK_SECRET"  # noqa: S105  # nosec B105
MINIMUM_SECRET_BYTES = 32


class SecurityConfig(ConfigurationModel):
    under_attack_secret: SecretStr = Field(
        default=SecretStr(""),
        alias=SECRET_VARIABLE,
        description=(
            "The fleet-wide key the Under Attack Mode challenge is signed with."
        ),
        json_schema_extra={"minimum_bytes": MINIMUM_SECRET_BYTES},
    )

    @field_validator("under_attack_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and len(secret.encode("utf-8")) < MINIMUM_SECRET_BYTES:
            raise ValueError(f"must contain at least {MINIMUM_SECRET_BYTES} bytes")
        return value

    @property
    def challenge_available(self) -> bool:
        return bool(self.under_attack_secret.get_secret_value())


CONFIGURATION = SecurityConfig.configuration("security")
