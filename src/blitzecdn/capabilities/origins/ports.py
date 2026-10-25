"""What the origin-check capability needs from the outside world."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.domain.runs import AnsibleRun


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope, without the deployment lock."""

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class OriginCheckRunner(Protocol):
    """The one play this capability runs; answers arrive on ``HostRun.report``."""

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class SiteReader(Protocol):
    """The read side of the site model, as the control plane publishes it."""

    def list_sites(self) -> list[CdnSite]: ...


class OriginProbe(Protocol):
    """Describing a site's origin to the edges; only ``to_probe`` is used here."""

    def to_probe(self, site: CdnSite) -> dict[str, object]: ...


__all__ = ["FleetPlaybooks", "OriginCheckRunner", "OriginProbe", "SiteReader"]
