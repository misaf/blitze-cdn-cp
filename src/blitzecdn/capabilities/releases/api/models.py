"""The HTTP shapes this capability publishes, restated from the domain models."""

from __future__ import annotations

from typing import Any, Literal

from blitzecdn.capabilities.releases.domain import EdgeReleaseState, SettingOrigin
from blitzecdn.core.domain.configuration import ConfigurationChangeKind
from blitzecdn.delivery.api.models import Model


class SettingSource(Model):
    setting: str
    value: Any
    origin: SettingOrigin
    rule: str | None = None


class HostExplanation(Model):
    host: str
    zone: str
    rule: str | None = None
    server_names: tuple[str, ...] = ()
    origin_host: str
    settings: tuple[SettingSource, ...] = ()


class ReleaseFinding(Model):
    source: str
    host: str | None = None
    message: str


class ArtifactDigest(Model):
    """An artifact's name and digest, without the document; fetch that separately."""

    name: str
    digest: str


class Artifact(ArtifactDigest):
    """One artifact as the edges were handed it.

    ``document`` is the artifact's own content, deliberately unmodelled: its
    shape belongs to the edge renderer that reads it, not to this API.
    """

    document: Any


class Release(Model):
    id: str
    digest: str
    compiler_version: int
    inputs_digest: str
    capabilities: tuple[str, ...] = ()
    servable: bool
    artifacts: tuple[ArtifactDigest, ...] = ()
    findings: tuple[ReleaseFinding, ...] = ()


class ReleaseDetail(Release):
    """One release with the parts a listing leaves out."""

    explanations: tuple[HostExplanation, ...] = ()


class ConfigurationChange(Model):
    """One rendered path that differs; ``diff`` is ``None`` for non-text content."""

    path: str
    change: ConfigurationChangeKind
    diff: str | None = None


class EdgeRelease(Model):
    """What deployment history records for one registered edge.

    ``state`` is ``recorded`` when the newest real run to touch the edge verified
    ``release`` there; ``unverified`` when a newer run failed or stopped on it
    after activating, ``release`` then being its last verified one; ``unknown``
    when no recorded run ever verified a release on it. ``deployment_id`` is the
    run that verified ``release``.
    """

    edge: str
    state: EdgeReleaseState
    release: str | None = None
    deployment_id: str | None = None


class ReleaseGroup(Model):
    """The edges recorded on one release, and whether deploying would change them.

    ``release`` of ``None`` groups the edges with no verified release; their
    ``changed`` is ``None``, there being nothing on record to compare against.
    """

    release: str | None = None
    edges: tuple[str, ...] = ()
    baseline: bool
    changed: bool | None = None


class FleetReleases(Model):
    """Every registered edge's recorded release, as deployment history has it.

    ``source`` is always ``deployment_records``: these are what deployments did,
    not live probes of what the edges serve now. ``mixed`` is true when the
    edges sit in more than one group; ``warnings`` says, in words, every way the
    single baseline does not describe the whole fleet, an empty fleet included.
    """

    source: Literal["deployment_records"]
    mixed: bool
    edges: tuple[EdgeRelease, ...] = ()
    releases: tuple[ReleaseGroup, ...] = ()
    warnings: tuple[str, ...] = ()


class ReleaseDiff(Model):
    """What deploying ``release`` would change on the edges, file by file.

    ``baseline_release`` is the one release the files are compared against: the
    release of the newest real run that reached any edge, ``None`` before any
    did. It need not be on every edge; ``fleet`` says which edges history places
    on which release and whether deploying would change each. ``before`` and
    ``after`` are the two bundles' digests.
    """

    baseline_release: str | None = None
    release: str
    fleet: FleetReleases
    changed: bool
    before: str | None = None
    after: str
    changes: tuple[ConfigurationChange, ...] = ()


__all__ = [
    "ArtifactDigest",
    "ConfigurationChange",
    "EdgeRelease",
    "FleetReleases",
    "HostExplanation",
    "Release",
    "ReleaseDetail",
    "ReleaseDiff",
    "ReleaseFinding",
    "ReleaseGroup",
    "SettingSource",
]
