"""Where "now" comes from.

A service that needs the time is handed a :data:`Clock` rather than reading the
system clock, so every timestamp a process writes comes from one place the
composition root chooses, and a test can say what time it is. Domain code takes
``now`` as an argument and never reads a clock at all.
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
