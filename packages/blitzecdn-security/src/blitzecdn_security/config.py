"""This capability's own configuration, read from what the controller was given.

A capability that needs a setting has to get it from somewhere, and the two
obvious answers are both wrong. A field on core's ``Settings`` would put the
name of an optional capability in the model every installation loads, whether
or not the distribution is attached — which is how ``under_attack_secret`` and
the MaxMind credentials came to live there. Reading ``os.environ`` directly
would miss the controller's ``.env``, which core merges and the process never
exports.

So the package reads ``Settings.capability_environment``: every ``BLITZE_*``
variable the control plane was configured with and does not consume itself,
already merged from the environment and the controller's ``.env``. Core does
not know this name belongs to this package; it forwards what it was given, here
and into the Ansible subprocess the role reads it from.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

__all__ = ["SECRET_VARIABLE", "SecurityConfig"]

#: The controller-side name of the fleet challenge secret. One constant, read
#: by this module and named in the role's defaults, so the two cannot drift.
SECRET_VARIABLE = "BLITZE_UNDER_ATTACK_SECRET"

#: Shortest key the challenge will sign with. An HMAC-SHA256 secret below its
#: block size buys nothing, and a short one here is nearly always a placeholder
#: somebody meant to replace.
_MINIMUM_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """What this capability needs from the controller's configuration."""

    under_attack_secret: SecretStr = SecretStr("")

    @classmethod
    def from_settings(cls, settings: object) -> SecurityConfig:
        """Read this package's settings off the control plane's.

        ``settings`` is core's ``Settings``; it is typed loosely here for the
        same reason the hook that hands it over is — this package depends on
        the control plane, and importing the concrete model to annotate one
        attribute read would buy nothing a test would catch.
        """
        environment: dict[str, SecretStr] = getattr(
            settings, "capability_environment", {}
        )
        return cls(under_attack_secret=environment.get(SECRET_VARIABLE, SecretStr("")))

    @property
    def challenge_available(self) -> bool:
        """Whether an edge could serve a challenge at all.

        Length is checked here as well as in the role because the two failures
        are different: the role refuses a converge it cannot complete, and this
        refuses a deployment before anything is rendered, naming the variable.
        """
        secret = self.under_attack_secret.get_secret_value()
        return len(secret.encode("utf-8")) >= _MINIMUM_SECRET_BYTES
