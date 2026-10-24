"""Pure capability policy and the runtime requirements derived from it.

Each policy value declares the capability tokens and setting names it requires.
Composition collects these requirements; providers and declared edge capabilities
determine whether a deployment can realize them. Keeping the rule with its value
avoids a second capability-specific decision table in the serving composition."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

__all__ = ["CapabilityPolicy"]


class CapabilityPolicy(BaseModel):
    """One capability's slice of a site's configuration.

    Subclasses override :attr:`capability_requirements` when some value they
    hold can ask for a runtime implementation. A contract whose
    every setting is an invariant of the managed edge — ``OriginPolicy`` — is
    answered by the default and says so by not overriding it.
    """

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Capability tokens this contract requests, and what asked for each.

        Empty means this policy adds no runtime capability requirements.
        """
        return {}

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Just the tokens, for a caller that has no message to write."""
        return frozenset(self.capability_requirements)
