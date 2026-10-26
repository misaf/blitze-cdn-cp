"""Certificate settings: one typed definition for loading and introspection."""

from __future__ import annotations

from typing import Final

from pydantic import Field, field_validator

from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "SETTINGS", "CertificateConfig"]

CERTBOT: Final = "BLITZE_CERTBOT"
ACME_DEFAULT_EMAIL: Final = "BLITZE_ACME_DEFAULT_EMAIL"
ACME_CA_DOMAIN: Final = "BLITZE_ACME_CA_DOMAIN"
RECONCILE_INTERVAL: Final = "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS"
RENEWAL_INTERVAL: Final = "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"
RENEWAL_BUDGET: Final = "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS"
SCAN_INTERVAL: Final = "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS"


class CertificateConfig(ConfigurationModel):
    """Controller settings only; domain TLS policy remains in canonical state."""

    certbot: str = Field(
        default="certbot",
        alias=CERTBOT,
        description="The certbot executable, by name on PATH or by absolute path.",
    )
    default_email: str | None = Field(
        default=None,
        alias=ACME_DEFAULT_EMAIL,
        description=(
            "The registration address ACME issuance uses when a request names "
            "none. Empty means every request must carry its own."
        ),
    )
    ca_domain: str = Field(
        default="letsencrypt.org",
        alias=ACME_CA_DOMAIN,
        description=(
            "The CA identity a CAA record has to name for issuance to be "
            "allowed. Change it with the ACME server certbot is pointed at."
        ),
    )
    reconcile_interval_seconds: int = Field(
        default=600,
        ge=0,
        le=86_400,
        alias=RECONCILE_INTERVAL,
        description="How often to reconcile installed certificates. 0 disables it.",
    )
    renewal_interval_seconds: int = Field(
        default=43_200,
        ge=0,
        le=604_800,
        alias=RENEWAL_INTERVAL,
        description="How often to sweep for certificates due renewal. 0 disables it.",
    )
    renewal_budget_seconds: int = Field(
        default=300,
        ge=30,
        le=7200,
        alias=RENEWAL_BUDGET,
        description=(
            "Wall-clock budget for one renewal sweep served over HTTP, after "
            "which it stops between sites and reports the rest as skipped."
        ),
    )
    scan_interval_seconds: int = Field(
        default=2_592_000,
        ge=0,
        le=31_536_000,
        alias=SCAN_INTERVAL,
        description="How often Automatic SSL/TLS rescans origins. 0 disables it.",
    )

    @field_validator("default_email", mode="before")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return (value.strip().lower() or None) if value is not None else None

    @field_validator("ca_domain")
    @classmethod
    def normalize_ca_domain(cls, value: str) -> str:
        candidate = value.strip().lower().rstrip(".")
        if (
            not candidate
            or "." not in candidate
            or any(character.isspace() for character in candidate)
        ):
            raise ValueError("must be a CA identity such as 'letsencrypt.org'")
        return candidate


CONFIGURATION = CertificateConfig.configuration("certificates")
# Kept for callers that inspect the existing individual setting declarations.
SETTINGS = CONFIGURATION.settings
