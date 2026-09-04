"""The base every capability contract inherits, and the question it answers.

A contract is a set of values describing how one capability is configured. The
one thing core needs to ask all of them alike is *which detachable
implementations do these values require* — because that is what the control
plane refuses a deployment over when a distribution is not installed, and the
answer has to be derivable from a stored site on a controller that has never
heard of the capability.

The answer is a mapping rather than a set. "Capability 'geoip' is not
installed" leaves an operator hunting for which of two unrelated switches asked
for it, so a token travels with the settings that requested it, named as the
stable schema names them: what the message says to change is what a patch would
set.

Declared by the contract that owns the setting, never by the composition. It
was the other way round — one ``if`` chain on ``SitePolicy`` restated every
capability's rule beside its own — which made ``sites`` the second place a
capability's requirement was written down, and the two could disagree without
anything saying so.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

__all__ = ["CapabilityPolicy"]


class CapabilityPolicy(BaseModel):
    """One capability's slice of a site's configuration.

    Subclasses override :attr:`capability_requirements` when some value they
    hold can ask for a detachable implementation. Most do; a contract whose
    every setting is an invariant of the managed edge — ``OriginPolicy`` — is
    answered by the default and says so by not overriding it.
    """

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Capability tokens this contract requests, and what asked for each.

        Empty means every value here is served by the control plane alone.
        """
        return {}

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Just the tokens, for a caller that has no message to write."""
        return frozenset(self.capability_requirements)
