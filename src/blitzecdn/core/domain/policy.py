"""Capability policy, and the runtime capabilities each policy value requires.

Keeping the requirement with the value avoids a separate decision table.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

__all__ = ["CapabilityPolicy"]


class CapabilityPolicy(BaseModel):
    """One capability's slice of a site's configuration.

    Subclasses override :attr:`capability_requirements` when a value they hold
    needs a runtime implementation.
    """

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Capability tokens this contract requests, and the settings that asked."""
        return {}

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Just the tokens, for a caller that has no message to write."""
        return frozenset(self.capability_requirements)
