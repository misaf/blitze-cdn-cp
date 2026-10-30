"""A capability's share of every site's serving policy.

Core never names a contract. Each feature contributes one under a group name,
and the serving capability composes the site policy from what was contributed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from blitzecdn.core.domain.policy import CapabilityPolicy

__all__ = ["PolicyCheck", "PolicyContribution", "PolicyView", "SiteCheck"]


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
