"""The HTTP shapes this capability publishes, restated from the domain models."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.releases.domain import SettingOrigin
from blitzecdn.entrypoints.api.models import Model


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


__all__ = [
    "ArtifactDigest",
    "HostExplanation",
    "Release",
    "ReleaseDetail",
    "ReleaseFinding",
    "SettingSource",
]
