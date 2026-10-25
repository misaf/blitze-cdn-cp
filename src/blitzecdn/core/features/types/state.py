"""A capability's share of the Ansible documents core renders.

Answered by ``blitzecdn_site_desired_state`` and
``blitzecdn_fleet_desired_state``. What a contribution may contain is bounded
by what ``yaml.safe_dump`` will write and the edge roles can read back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "FleetStateContribution",
    "SiteStateContribution",
    "StateValue",
]


#: What a contribution is allowed to be. Ansible variables are YAML, so this is
#: what `yaml.safe_dump` will accept and what the edge roles can read back.
type StateValue = (
    str
    | int
    | float
    | bool
    | Mapping[str, "StateValue"]
    | tuple["StateValue", ...]
    | list["StateValue"]
    | None
)


@dataclass(frozen=True, slots=True)
class SiteStateContribution:
    """One feature's share of the Ansible document for one virtual host.

    `overrides` names the variables this feature deliberately replaces; see
    :func:`~blitzecdn.core.features.merge_variables`.
    """

    feature: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FleetStateContribution:
    """Variables about the fleet as a whole rather than about one site.

    Derived from every site at once: which one site carries `reuseport` on the
    QUIC listener is not something any single site knows.
    """

    feature: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)
