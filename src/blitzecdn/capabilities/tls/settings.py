"""Certificate settings: one typed definition for loading and introspection."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, field_validator, model_validator

from blitzecdn.core.domain.validation import ip_literal
from blitzecdn.core.features import ConfigurationModel

__all__ = ["CONFIGURATION", "SETTINGS", "CertificateConfig", "certificate_dir"]

CERTBOT: Final = "BLITZE_CERTBOT"
ACME_DEFAULT_EMAIL: Final = "BLITZE_ACME_DEFAULT_EMAIL"
ACME_CA_DOMAIN: Final = "BLITZE_ACME_CA_DOMAIN"
RECONCILE_INTERVAL: Final = "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS"
RENEWAL_INTERVAL: Final = "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"
RENEWAL_BUDGET: Final = "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS"
PREFLIGHT_DNS_TIMEOUT: Final = "BLITZE_PREFLIGHT_DNS_TIMEOUT_SECONDS"
PREFLIGHT_DNS_SERVERS: Final = "BLITZE_PREFLIGHT_DNS_SERVERS"
PREFLIGHT_DNS_SOURCE: Final = "BLITZE_PREFLIGHT_DNS_SOURCE"

#: Installed certificate material, one directory per site, under the
#: controller's state directory. Not configurable: backup and restore carry it
#: by this name.
_CERTIFICATE_DIRECTORY: Final = "certificates"


def certificate_dir(state_dir: Path) -> Path:
    """Where installed certificate material lives under the state directory."""
    return state_dir / _CERTIFICATE_DIRECTORY


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
            "Wall-clock budget for one renewal sweep, scheduled or queued "
            "through the API, after which it stops between sites and reports "
            "the rest as skipped."
        ),
    )
    preflight_dns_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        alias=PREFLIGHT_DNS_TIMEOUT,
        description=(
            "Budget for one CAA lookup in certificate preflight; past it, the "
            "check becomes advisory rather than blocking."
        ),
    )
    preflight_dns_servers: str = Field(
        default="",
        alias=PREFLIGHT_DNS_SERVERS,
        description=(
            "Comma-separated recursive resolver addresses preflight asks instead "
            "of each zone's authoritative nameservers. Empty follows "
            "preflight_dns_source."
        ),
    )
    preflight_dns_source: Literal["authoritative", "host"] = Field(
        default="authoritative",
        alias=PREFLIGHT_DNS_SOURCE,
        description=(
            "Where preflight looks names up when no servers are configured: "
            "'authoritative' asks each zone's own nameservers, as the CA does; "
            "'host' uses the controller's resolver, its caches and /etc/hosts."
        ),
    )

    @property
    def dns_servers(self) -> tuple[str, ...]:
        """The configured preflight resolvers, in order; empty when none are."""
        return tuple(filter(None, self.preflight_dns_servers.split(",")))

    @field_validator("preflight_dns_servers")
    @classmethod
    def validate_dns_servers(cls, value: str) -> str:
        """Addresses only: a hostname would need the resolver we distrust."""
        servers = []
        for part in filter(str.strip, value.split(",")):
            try:
                servers.append(str(ip_literal(part.strip())))
            except ValueError as exc:
                raise ValueError(
                    f"preflight DNS server {part.strip()!r} is not an IP address"
                ) from exc
        return ",".join(servers)

    @model_validator(mode="after")
    def check_preflight_dns(self) -> Self:
        """Servers and the host resolver are two answers to one question."""
        if self.preflight_dns_servers and self.preflight_dns_source == "host":
            raise ValueError(
                "preflight_dns_servers and preflight_dns_source = 'host' "
                "contradict each other; set one"
            )
        return self

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
