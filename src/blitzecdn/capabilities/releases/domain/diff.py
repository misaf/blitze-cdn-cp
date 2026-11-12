"""What deploying a release would change on the edges, as rendered files.

The comparison has one baseline, but the fleet need not run one release: a
canary moves one edge, and a rollout that fails after earlier batches committed
leaves those edges ahead of the rest. So a diff also carries what deployment
history records for every registered edge, grouped by release, and says plainly
when the edges disagree. Those are recorded observations, not live probes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from blitzecdn.capabilities.releases.domain.release import Release
from blitzecdn.core.domain.configuration import ConfigurationDiff

__all__ = [
    "FLEET_OBSERVATION_SOURCE",
    "EdgeRelease",
    "EdgeReleaseState",
    "FleetReleases",
    "ReleaseDiff",
    "ReleaseGroup",
]

#: Where per-edge releases come from. Named in every diff so nobody reads a
#: record of what a deployment did as a probe of what an edge serves now.
FLEET_OBSERVATION_SOURCE = "deployment_records"


class EdgeReleaseState(StrEnum):
    """How far deployment history vouches for the release recorded on an edge."""

    #: The newest real run to touch the edge left it verified on ``release``.
    RECORDED = "recorded"
    #: A newer real run failed or stopped on the edge after activating, so what
    #: it serves is not verified; ``release`` is its last verified one, if any.
    UNVERIFIED = "unverified"
    #: No recorded real run ever verified a release on the edge: it was never
    #: deployed, or its history was pruned.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EdgeRelease:
    """What deployment history says one edge runs.

    ``deployment`` is the run that verified ``release`` there, ``None`` with it.
    """

    edge: str
    state: EdgeReleaseState
    release: str | None = None
    deployment: str | None = None

    def summary(self) -> dict[str, Any]:
        """The edge as one JSON-ready document."""
        return {
            "edge": self.edge,
            "state": self.state.value,
            "release": self.release,
            "deployment_id": self.deployment,
        }


@dataclass(frozen=True, slots=True)
class ReleaseGroup:
    """The edges recorded on one release, and whether deploying would change them.

    ``release`` of ``None`` collects edges with no verified release, whose
    ``changed`` is ``None``: there is nothing on record to compare against.
    """

    release: str | None
    edges: tuple[str, ...]
    baseline: bool
    changed: bool | None

    def summary(self) -> dict[str, Any]:
        """The group as one JSON-ready document."""
        return {
            "release": self.release,
            "edges": list(self.edges),
            "baseline": self.baseline,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class FleetReleases:
    """Every registered edge's recorded release, against the diff's baseline."""

    baseline: str | None
    edges: tuple[EdgeRelease, ...]
    groups: tuple[ReleaseGroup, ...]

    @classmethod
    def observe(
        cls,
        baseline: str | None,
        fleet: Sequence[str],
        recorded: Mapping[str, EdgeRelease],
        changed: Mapping[str, bool],
    ) -> FleetReleases:
        """Place every edge in ``fleet``; one without a record is ``UNKNOWN``.

        ``changed`` says, per recorded release, whether the desired state differs
        from it. Edges recorded but no longer registered are left out.
        """
        edges = tuple(
            recorded.get(name, EdgeRelease(name, EdgeReleaseState.UNKNOWN))
            for name in sorted(fleet)
        )
        members: dict[str | None, list[str]] = {}
        for edge in edges:
            members.setdefault(edge.release, []).append(edge.edge)
        # The baseline first, the edges with nothing verified last.
        order = sorted(members, key=lambda r: (r != baseline, r is None, r or ""))
        groups = tuple(
            ReleaseGroup(
                release=release,
                edges=tuple(members[release]),
                baseline=release is not None and release == baseline,
                changed=None if release is None else changed[release],
            )
            for release in order
        )
        return cls(baseline=baseline, edges=edges, groups=groups)

    @property
    def mixed(self) -> bool:
        """Whether the edges span groups; those with nothing verified are one."""
        return len(self.groups) > 1

    def _named(self, state: EdgeReleaseState) -> list[str]:
        return [edge.edge for edge in self.edges if edge.state is state]

    def warnings(self) -> tuple[str, ...]:
        """Every way the single baseline does not describe the whole fleet."""
        if not self.edges:
            return (
                "no edges are registered; the baseline comes from deployment "
                "history alone",
            )
        notes = []
        behind = [
            edge.edge
            for edge in self.edges
            if edge.release is not None and edge.release != self.baseline
        ]
        if self.mixed:
            releases = sum(1 for group in self.groups if group.release is not None)
            notes.append(
                f"the fleet is mixed: {releases} release(s) are recorded across "
                f"{len(self.edges)} edge(s); the diff is against "
                f"{self.baseline or 'nothing deployed'} only"
            )
        if behind:
            notes.append(
                f"edge(s) on another release than the baseline: {', '.join(behind)}"
            )
        if unverified := self._named(EdgeReleaseState.UNVERIFIED):
            notes.append(
                "edge(s) whose newest run failed or stopped after activating, so "
                f"what they serve is unverified: {', '.join(unverified)}"
            )
        if unknown := self._named(EdgeReleaseState.UNKNOWN):
            notes.append(
                "edge(s) with no verified release on record: " + ", ".join(unknown)
            )
        return tuple(notes)

    def summary(self) -> dict[str, Any]:
        """The fleet as one JSON-ready document."""
        return {
            "source": FLEET_OBSERVATION_SOURCE,
            "mixed": self.mixed,
            "edges": [edge.summary() for edge in self.edges],
            "releases": [group.summary() for group in self.groups],
            "warnings": list(self.warnings()),
        }


@dataclass(frozen=True, slots=True)
class ReleaseDiff:
    """The rendered tree of ``release`` against one baseline the fleet ran.

    ``baseline`` is the release the newest real run that reached any edge
    applied, or ``None`` before any has, in which case every file is new. It
    need not be on every edge: ``fleet`` says which edges history places on
    which release. Secrets never appear: certificates and keys are delivered
    apart from the bundle.
    """

    baseline: str | None
    release: Release
    configuration: ConfigurationDiff
    fleet: FleetReleases

    def summary(self) -> dict[str, Any]:
        """The diff as one JSON-ready document, the shape both surfaces publish."""
        return {
            "baseline_release": self.baseline,
            "release": self.release.id,
            "fleet": self.fleet.summary(),
            **self.configuration.summary(),
        }
