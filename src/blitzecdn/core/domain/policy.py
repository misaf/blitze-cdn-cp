"""Capability policy, and the runtime capabilities each policy value requires.

Keeping the requirement with the value avoids a separate decision table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from blitzecdn.core.exceptions import FeatureError

__all__ = [
    "CapabilityPolicy",
    "PolicyCheck",
    "PolicyView",
    "ResolvedPolicySchema",
    "SiteCheck",
]


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


class PolicyView(Protocol):
    """What a check may read of a composed policy: one contract at a time."""

    def __getitem__[P: CapabilityPolicy](self, contract: type[P]) -> P: ...


#: A rule spanning contracts, such as HTTP/3 requiring edge TLS. It raises
#: ``ValueError`` so the policy's validation reports it like any other field.
type PolicyCheck = Callable[[PolicyView], None]

#: A rule that also needs the virtual host's name, so it holds for a host but
#: not for a zone, which yields several. Raises ``ValueError`` as well.
type SiteCheck = Callable[[str, PolicyView], None]


@dataclass(frozen=True, slots=True)
class ResolvedPolicySchema:
    """Every contributed contract by group, and which group owns each setting."""

    #: In feature-name order, so the same features always compose one policy.
    groups: Mapping[str, type[CapabilityPolicy]]
    #: Flat setting name to the group whose contract declares it.
    setting_owners: Mapping[str, str]
    checks: tuple[PolicyCheck, ...]
    site_checks: tuple[SiteCheck, ...]

    def group_of(self, contract: type[CapabilityPolicy]) -> str:
        """The group a contract is stored under."""
        for group, registered in self.groups.items():
            if registered is contract:
                return group
        raise FeatureError(
            f"{contract.__name__} is not part of the site policy; the feature "
            "that owns it has to contribute it as a `PolicyContribution`"
        )
