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
    """One plugin's share of the Ansible document for one virtual host.

    `overrides` is what makes merging order-independent. Two plugins writing
    the same variable is a conflict unless exactly one of them says it is
    replacing a value another produced — `certificates` deliberately replaces
    the certificate paths projected from the site model, and saying so is the
    difference between a designed override and whichever plugin loaded last.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FleetStateContribution:
    """Variables about the fleet as a whole rather than about one site.

    Separate from `SiteStateContribution` because it is derived from every
    site at once: which single site carries `reuseport` on the QUIC listener
    is not a fact any one site knows about itself.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)
