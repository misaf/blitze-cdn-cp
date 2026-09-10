"""Compiling a release, and the service that stores what it compiled."""

from __future__ import annotations

from blitzecdn.capabilities.releases.service.compiler import compile_release
from blitzecdn.capabilities.releases.service.releases import ReleaseService

__all__ = ["DESIRED_STATE_ARTIFACT", "ReleaseService", "compile_release"]
