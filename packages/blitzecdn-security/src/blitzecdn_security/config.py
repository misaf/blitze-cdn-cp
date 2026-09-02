"""This capability's own configuration, read from what the controller was given.

A capability that needs a setting has to get it from somewhere, and the two
obvious answers are both wrong. A field on core's ``Settings`` would put the
name of an optional capability in the model every installation loads, whether
or not the distribution is attached — which is how ``under_attack_secret`` and
the MaxMind credentials came to live there. Reading ``os.environ`` directly
would miss the controller's ``.env``, which core merges and the process never
exports.

So this package declares the key it owns in its Ansible contribution, and core
hands back exactly that: ``platform.capability_config.for_plugin("security")``,
a ``CapabilityConfig`` holding this package's keys and no other package's,
already merged from the environment and the controller's ``.env``. Composition
rejects an unclaimed or multiply claimed name and forwards only resolved
package-owned keys to the Ansible subprocess.

The length rule travels with the declaration rather than living here, which is
why this module no longer enforces it. A secret that is present but too short
is a value an operator can fix, and core now refuses it at composition — before
a service is built, naming the key and this capability — instead of leaving it
to be discovered by the first site that turns Under Attack Mode on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr

from blitzecdn.core.plugins import CapabilityConfig

__all__ = ["MINIMUM_SECRET_BYTES", "SECRET_VARIABLE", "SecurityConfig"]

#: The controller-side *name* of the fleet challenge secret — never its value,
#: which is only ever read from the environment. One constant, spelled here and
#: in the role's defaults, so the two cannot drift.
#:
#: The linters see a capitalised string with "SECRET" in it and assume a
#: hardcoded credential; both are told otherwise here rather than by renaming
#: the constant into something that describes it less well.
SECRET_VARIABLE = "BLITZE_UNDER_ATTACK_SECRET"  # noqa: S105  # nosec B105

#: Shortest key the challenge will sign with. An HMAC-SHA256 secret below its
#: block size buys nothing, and a short one here is nearly always a placeholder
#: somebody meant to replace. Declared to core on the contribution beside this
#: module, which is what enforces it; public so the two cannot drift.
MINIMUM_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """What this capability needs from the controller's configuration."""

    under_attack_secret: SecretStr = field(default_factory=lambda: SecretStr(""))

    @classmethod
    def from_capability_config(cls, config: CapabilityConfig) -> SecurityConfig:
        """Read this package's own configuration, typed and already scoped.

        ``config`` holds the keys this package declared and nothing else, so
        the name below is checked against the declaration rather than looked up
        hopefully in a dictionary of everything the controller was given.
        """
        return cls(under_attack_secret=config.secret(SECRET_VARIABLE))

    @property
    def challenge_available(self) -> bool:
        """Whether an edge could serve a challenge at all.

        Presence only. A secret shorter than :data:`MINIMUM_SECRET_BYTES` never
        reaches here — core refuses the controller's configuration at
        composition against the length this package declared — so the one
        remaining case is a controller that set nothing, which is a supported
        state and not an error: it is only a deployment this capability has to
        refuse, and only for a site that asks for the challenge.
        """
        return bool(self.under_attack_secret.get_secret_value())
