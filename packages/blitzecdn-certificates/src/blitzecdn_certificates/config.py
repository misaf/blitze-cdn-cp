"""This capability's own configuration, read from what the controller was given.

Seven names, and every one of them used to be a field on core's ``Settings``:
``certbot``, ``acme_default_email``, ``acme_ca_domain``, three intervals and a
renewal budget. That is the largest single instance of the thing the rule
against it describes — a core distribution carrying the configuration of a
wheel that may not be installed, and an operator of a controller with no
``blitzecdn-certificates`` attached still being offered somewhere to set an
ACME email.

They stayed there longer than the secrets did for one reason: until
:class:`~blitzecdn.core.plugins.CapabilitySetting` existed, the only thing a
capability could declare was an ``EnvironmentKey``, and none of these is a
secret. A renewal interval is exactly the sort of non-secret default that
belongs in ``blitzecdn.toml``, and the mechanism that could hold one had not
been built — so ``Settings`` held them, and the rule held for
``blitzecdn-security`` alone, whose whole configuration happens to be a
credential.

The module is shaped like ``blitzecdn_security``'s deliberately: names as
module constants so the declaration and the read cannot drift, one frozen
dataclass, and one ``from_capability_config`` that turns the scoped
``CapabilityConfig`` core hands back into it.

What did *not* move is ``certificate_dir``. Two distributions read it —
this one writes the chains and ``blitzecdn-backup`` archives them — so it is
genuinely shared state rather than this capability's setting, and moving it
here would break backup on the day it was moved. Settling that ownership is
the backup-component question, not this one.
"""

from __future__ import annotations

from dataclasses import dataclass

from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.plugins import CapabilityConfig, CapabilitySetting

__all__ = ["SETTINGS", "CertificateConfig"]

#: The controller-side names this package owns. Spelled once here, read from
#: the declaration below and from :meth:`CertificateConfig.from_capability_config`,
#: so a rename cannot half-happen.
CERTBOT = "BLITZE_CERTBOT"
ACME_DEFAULT_EMAIL = "BLITZE_ACME_DEFAULT_EMAIL"
ACME_CA_DOMAIN = "BLITZE_ACME_CA_DOMAIN"
RECONCILE_INTERVAL = "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS"
RENEWAL_INTERVAL = "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"
RENEWAL_BUDGET = "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS"
SCAN_INTERVAL = "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS"

#: What this capability asks an operator to configure, with the bounds core can
#: enforce without knowing what any of it means. The bounds are the ones the
#: `Settings` fields carried, kept rather than re-derived: a zero interval
#: disables its job, which is how every one of them is turned off, and the
#: ceilings are the point past which a "recurring" job has stopped recurring.
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
    #: ``None`` rather than the empty string the setting resolves to. A request
    #: that names no address and finds none configured has to be refused, and
    #: the service says so by testing for a missing value — which an empty
    #: string is not, in the one language where it would matter.
    default_email: str | None = None

    @classmethod
    def from_capability_config(cls, config: CapabilityConfig) -> CertificateConfig:
        """Read this package's own configuration, typed and already scoped.

        ``config`` holds the names this package declared and nothing else, so
        each read below is checked against the declaration rather than looked
        up hopefully in a dictionary of everything the controller was given.
        """
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
    """Reject an empty or malformed CA identity rather than blocking issuance.

    The CAA check asks whether this name appears in the record's allowed
    issuers. Empty never appears, so an unset value would turn a passing
    preflight into a permanent, unexplained refusal.

    This is the validation core deliberately does not do. Presence and length
    are the two rules a control plane can apply to a value it cannot interpret;
    "is this a CA identity" needs to know what the value is *for*, which is
    knowledge of the capability that asks a CA for a certificate.
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
