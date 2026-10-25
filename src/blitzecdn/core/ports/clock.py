"""Where "now" comes from.

Services are handed a :data:`Clock` so the composition root and tests decide
the time; domain code takes ``now`` as an argument.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

__all__ = ["Clock", "system_clock"]

#: A source of timezone-aware UTC instants.
Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """The wall clock, in UTC. The default every service falls back to."""
    return datetime.now(UTC)
