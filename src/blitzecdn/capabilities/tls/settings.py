"""TLS controller settings: certbot, ACME identity, intervals and budgets.

Shared certificate storage paths stay in core ``Settings``; backup uses them too.
"""

from __future__ import annotations

from dataclasses import dataclass

from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.features import CapabilityConfig, CapabilitySetting

__all__ = ["SETTINGS", "CertificateConfig"]

#: The controller-side names this capability owns, spelled once.
CERTBOT = "BLITZE_CERTBOT"
ACME_DEFAULT_EMAIL = "BLITZE_ACME_DEFAULT_EMAIL"
ACME_CA_DOMAIN = "BLITZE_ACME_CA_DOMAIN"
RECONCILE_INTERVAL = "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS"
RENEWAL_INTERVAL = "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"
RENEWAL_BUDGET = "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS"
SCAN_INTERVAL = "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS"

#: What this capability asks an operator to configure. A zero interval
#: disables its job.
SETTINGS = (
    CapabilitySetting(
        name=CERTBOT,
        default="certbot",
        summary="The certbot executable, by name on PATH or by absolute path.",
    ),
    CapabilitySetting(
        name=ACME_DEFAULT_EMAIL,
        default="",
        summary=(
            "The registration address ACME issuance uses when a request names "
            "none. Empty means every request must carry its own."
        ),
    ),
    CapabilitySetting(
        name=ACME_CA_DOMAIN,
        default="letsencrypt.org",
        summary=(
            "The CA identity a CAA record has to name for issuance to be "
            "allowed. Change it with the ACME server certbot is pointed at."
        ),
    ),
    CapabilitySetting(
        name=RECONCILE_INTERVAL,
        default=600,
        minimum=0,
        maximum=86_400,
        summary="How often to reconcile installed certificates. 0 disables it.",
    ),
    CapabilitySetting(
        name=RENEWAL_INTERVAL,
        default=43_200,
        minimum=0,
        maximum=604_800,
        summary="How often to sweep for certificates due renewal. 0 disables it.",
    ),
    CapabilitySetting(
        name=RENEWAL_BUDGET,
        default=300,
        minimum=30,
        maximum=7200,
        summary=(
            "Wall-clock budget for one renewal sweep served over HTTP, after "
            "which it stops between sites and reports the rest as skipped."
        ),
    ),
    CapabilitySetting(
        name=SCAN_INTERVAL,
        default=2_592_000,
        minimum=0,
        maximum=31_536_000,
        summary="How often Automatic SSL/TLS rescans origins. 0 disables it.",
    ),
)


@dataclass(frozen=True, slots=True)
class CertificateConfig:
    """What this capability needs from the controller's configuration."""

    certbot: str
    ca_domain: str
    reconcile_interval_seconds: int
    renewal_interval_seconds: int
    renewal_budget_seconds: int
    scan_interval_seconds: int
    #: ``None`` rather than ``""``, so the service can refuse a missing address.
    default_email: str | None = None

    @classmethod
    def from_capability_config(cls, config: CapabilityConfig) -> CertificateConfig:
        """Read this capability's own scoped configuration, typed."""
        email = config.text(ACME_DEFAULT_EMAIL).strip().lower()
        return cls(
            certbot=config.text(CERTBOT),
            ca_domain=_ca_domain(config.text(ACME_CA_DOMAIN)),
            reconcile_interval_seconds=config.integer(RECONCILE_INTERVAL),
            renewal_interval_seconds=config.integer(RENEWAL_INTERVAL),
            renewal_budget_seconds=config.integer(RENEWAL_BUDGET),
            scan_interval_seconds=config.integer(SCAN_INTERVAL),
            default_email=email or None,
        )


def _ca_domain(value: str) -> str:
    """Reject an empty or malformed CA identity.

    The CAA check looks for this name among allowed issuers; an empty one would
    make every preflight fail without explanation.
    """
    candidate = value.strip().lower().rstrip(".")
    if (
        not candidate
        or "." not in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise ConfigurationError(
            f"{ACME_CA_DOMAIN} must be a CA identity such as 'letsencrypt.org'"
        )
    return candidate
