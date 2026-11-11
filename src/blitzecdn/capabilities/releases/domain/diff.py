"""What deploying a release would change on the edges, as rendered files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blitzecdn.capabilities.releases.domain.release import Release
from blitzecdn.core.domain.configuration import ConfigurationDiff

__all__ = ["ReleaseDiff"]


@dataclass(frozen=True, slots=True)
class ReleaseDiff:
    """The rendered tree of ``release`` against the one the fleet is running.

    ``live`` is the release the newest real run that reached an edge applied, or
    ``None`` before any has, in which case every file is new. Secrets never
    appear: certificates and keys are delivered apart from the bundle.
    """

    live: str | None
    release: Release
    configuration: ConfigurationDiff

    def summary(self) -> dict[str, Any]:
        """The diff as one JSON-ready document, the shape both surfaces publish."""
        return {
            "live_release": self.live,
            "release": self.release.id,
            **self.configuration.summary(),
        }
