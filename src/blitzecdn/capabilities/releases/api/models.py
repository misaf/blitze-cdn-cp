"""The HTTP shapes this capability publishes, restated from the domain models."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.releases.domain import SettingOrigin
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


class ReleaseDiff(Model):
    """What deploying ``release`` would change on the edges, file by file.

    ``live_release`` is what the fleet is running, ``None`` before any deployment
    reached an edge; ``before`` and ``after`` are the two bundles' digests.
    """

    live_release: str | None = None
    release: str
    changed: bool
    before: str | None = None
    after: str
    changes: tuple[ConfigurationChange, ...] = ()


__all__ = [
    "ArtifactDigest",
    "ConfigurationChange",
    "HostExplanation",
    "Release",
    "ReleaseDetail",
    "ReleaseDiff",
    "ReleaseFinding",
    "SettingSource",
]
