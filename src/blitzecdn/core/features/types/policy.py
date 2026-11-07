"""A capability's share of every site's serving policy.

Core never names a contract. Each feature contributes one under a group name,
and composition binds those contributions to the shared site policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from blitzecdn.core.domain.policy import (
    CapabilityPolicy,
    PolicyCheck,
    PolicyView,
    SiteCheck,
)

__all__ = ["PolicyCheck", "PolicyContribution", "PolicyView", "SiteCheck"]


@dataclass(frozen=True, slots=True)
class PolicyContribution:
    """One contract a feature adds to every site's policy.

    ``group`` is the contract's key in the stored policy document. A check may
    read only its own contract and those in ``depends_on``, which must be
    contributed too, or a check would fail on a site rather than at startup.
    ``checks`` hold wherever a policy is, ``site_checks`` only on a host.
    """

    feature: str
    group: str
    contract: type[CapabilityPolicy]
    depends_on: tuple[type[CapabilityPolicy], ...] = ()
    checks: tuple[PolicyCheck, ...] = ()
    site_checks: tuple[SiteCheck, ...] = ()
