"""The HTTP shapes this capability publishes.

Restated here rather than serialising the domain models directly, for the
reason every capability's ``api`` package restates its own: a published schema
is a contract with clients outside this repository, and a domain model is free
to change. The two are the same today and the freezing test in
``tests/contract/frozen`` is what will say so when they stop being.
"""

from __future__ import annotations

from typing import Any

from blitzecdn.api.models import Model
from blitzecdn.capabilities.releases.domain import SettingOrigin


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
    """An artifact's name and digest, without the document itself.

    The listing shape. A desired-state document carries every site's full
    policy, so returning one inline everywhere would make ``GET /v1/releases``
    grow with the fleet; the document is available from the artifact route,
    which a client asks for when it actually wants to diff one.
    """

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
