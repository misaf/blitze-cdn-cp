"""A change another capability asks to make to the zone or rule behind a host."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from blitzecdn.core.domain.events import DomainEvent

__all__ = ["SiteChange"]


@dataclass(frozen=True, slots=True)
class SiteChange:
    """Flat settings to write, and what the audit trail records alongside them.

    ``settings`` are written where the host's policy comes from: its zone, or
    the overrides of the rule that produced it.
    """

    settings: Mapping[str, Any]
    event: DomainEvent | None = field(default=None)
