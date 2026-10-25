"""This capability's own configuration, from its scoped ``CapabilityConfig``.

Core merges the environment and ``.env`` and hands back only the keys this
capability declared; the secret's minimum length is enforced at composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr

from blitzecdn.core.features import CapabilityConfig

__all__ = ["MINIMUM_SECRET_BYTES", "SECRET_VARIABLE", "SecurityConfig"]

#: The challenge secret's variable name, never its value; shared with the
#: role defaults. Not a hardcoded credential, whatever the linters think.
SECRET_VARIABLE = "BLITZE_UNDER_ATTACK_SECRET"  # noqa: S105  # nosec B105

#: Shortest signing key accepted: shorter HMAC-SHA256 keys are usually
#: placeholders. Declared to core, which enforces it.
MINIMUM_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """What this capability needs from the controller's configuration."""

    under_attack_secret: SecretStr = field(default_factory=lambda: SecretStr(""))

    @classmethod
    def from_capability_config(cls, config: CapabilityConfig) -> SecurityConfig:
        """Read this capability's own scoped configuration, typed."""
        return cls(under_attack_secret=config.secret(SECRET_VARIABLE))

    @property
    def challenge_available(self) -> bool:
        """Whether an edge could serve a challenge: a secret is configured.

        Length was checked at composition; an absent secret is legal and only refuses
        sites that ask for the challenge.
        """
        return bool(self.under_attack_secret.get_secret_value())
