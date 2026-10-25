"""Resolve live contributions once, before the deterministic compiler.

Certificate stores and preflight checks may inspect the world; their answers
are captured as values, so a serialized Compilation replays without services.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from blitzecdn.capabilities.dns.domain import derive_hosts
from blitzecdn.capabilities.releases.domain.compilation import Compilation
from blitzecdn.capabilities.releases.domain.inputs import ReleaseInputs
from blitzecdn.capabilities.releases.domain.release import (
    Artifact,
    CompiledSite,
    EdgeCapabilities,
)
from blitzecdn.capabilities.releases.ports import SiteChecks, StateContributors


def prepare_compilation(
    inputs: ReleaseInputs,
    *,
    capabilities: Iterable[str],
    targets: Sequence[EdgeCapabilities],
    contributors: StateContributors,
    site_checks: SiteChecks,
    allow_empty_sites: bool,
) -> Compilation:
    """Capture state before calling any contributor that can inspect live data."""
    canonical_inputs = inputs.encode()
    captured = ReleaseInputs.decode(canonical_inputs)
    sites = tuple(
        site
        for site in derive_hosts(
            list(captured.domains), list(captured.rules), list(captured.records)
        )
        if site.serves_traffic
    )
    objections = tuple(finding for site in sites for finding in site_checks(site))
    # A refused site never reaches a contributor that needs the missing resource;
    # its policy is still captured for diagnostics.
    compiled = tuple(
        CompiledSite.from_variables(
            site, {} if objections else contributors.site_variables(site)
        )
        for site in sites
    )
    fleet = {} if objections else contributors.fleet_variables(sites)
    return Compilation(
        canonical_inputs=canonical_inputs,
        capabilities=tuple(sorted(set(capabilities))),
        targets=tuple(sorted(targets, key=lambda target: target.name)),
        sites=compiled,
        fleet_variables=Artifact.from_document("fleet-variables", fleet),
        objections=objections,
        allow_empty_sites=allow_empty_sites,
    )
