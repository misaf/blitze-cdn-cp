"""Compile canonical state into the release the fleet is converged from.

A pure function: no database, socket, file or clock, so equal inputs give equal
digests. A release that cannot be converged is still compiled and carries every
reason, so ``validate`` reports all problems at once.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from blitzecdn.capabilities.dns.domain import unservable_hosts

# Imported from its own module: only provenance reconstruction needs it.
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.releases.domain import (
    COMPILER_VERSION,
    DESIRED_STATE_ARTIFACT,
    Artifact,
    EdgeCapabilities,
    HostExplanation,
    Release,
    ReleaseFinding,
    ReleaseInputs,
    explain,
)
from blitzecdn.capabilities.releases.domain.compilation import Compilation
from blitzecdn.capabilities.serving.domain import CdnSite

__all__ = ["compile_release"]


def compile_release(compilation: Compilation) -> Release:
    """Compile captured values without invoking features or other live code."""
    inputs = compilation.inputs
    installed = compilation.capabilities
    ordered_targets = compilation.targets
    sites = tuple(entry.site for entry in compilation.sites)
    findings = list(_derivation_findings(inputs))
    for site in sites:
        findings.extend(_capability_findings(site, installed, ordered_targets))
    findings.extend(compilation.objections)
    compiled = compilation.sites
    derived = {
        **compilation.fleet_variables.document,
        "blitzecdn_nginx_allow_empty_sites": compilation.allow_empty_sites,
        "blitzecdn_nginx_sites": [entry.variables for entry in compiled],
    }
    conflicts = tuple(_setting_conflicts(inputs, derived))
    findings.extend(conflicts)
    if compilation.objections or conflicts:
        return Release(
            compiler_version=COMPILER_VERSION,
            inputs_digest=inputs.digest,
            capabilities=installed,
            targets=ordered_targets,
            explanations=_explanations(inputs, sites),
            findings=tuple(findings),
        )
    # Settings first, for reading order; collisions are findings, not precedence.
    artifact = Artifact.from_document(
        name=DESIRED_STATE_ARTIFACT, document={**inputs.settings, **derived}
    )
    return Release(
        compiler_version=COMPILER_VERSION,
        inputs_digest=inputs.digest,
        capabilities=installed,
        targets=ordered_targets,
        sites=compiled,
        artifacts=(artifact,),
        explanations=_explanations(inputs, sites),
        findings=tuple(findings),
    )


def _setting_conflicts(
    inputs: ReleaseInputs, derived: dict[str, object]
) -> Iterable[ReleaseFinding]:
    """Fleet settings naming a variable the compiler or a feature derives; refused."""
    for name in sorted(set(inputs.settings) & set(derived)):
        yield ReleaseFinding(
            source="settings",
            host=None,
            message=(
                f"fleet setting {name!r} names a variable this release derives; "
                f"remove it with `blitzecdn config unset {name}`"
            ),
        )


def _derivation_findings(inputs: ReleaseInputs) -> Iterable[ReleaseFinding]:
    """Groups whose policy no longer composes, which the derivation dropped silently."""
    for refused in unservable_hosts(
        list(inputs.domains), list(inputs.rules), list(inputs.records)
    ):
        yield ReleaseFinding(
            source="derivation", host=refused.name, message=refused.message
        )


def _capability_findings(
    site: CdnSite,
    installed: tuple[str, ...],
    targets: Sequence[EdgeCapabilities],
) -> Iterable[ReleaseFinding]:
    """What this site asks for that the controller or a targeted edge lacks.

    Missing on the controller, the artifact silently omits the settings; missing on
    an edge, the edge cannot execute the artifact (``brotli on`` without the
    module). An edge that declared nothing is assumed to have what the controller
    has.
    """
    requested = site.policy.capability_requirements
    for token in sorted(set(requested) - set(installed)):
        settings = ", ".join(requested[token])
        yield ReleaseFinding(
            source="capabilities",
            host=site.name,
            message=(
                f"capability {token!r} is not installed, and this site's "
                f"{settings} {'requests' if len(requested[token]) == 1 else 'request'}"
                " it; no feature on this controller provides it, so disable "
                "the setting that requests it"
            ),
        )
    for target in targets:
        if target.capabilities is None:
            continue
        for token in sorted(set(requested) - set(target.capabilities)):
            settings = ", ".join(requested[token])
            yield ReleaseFinding(
                source="capabilities",
                host=site.name,
                message=(
                    f"edge {target.name!r} does not provide capability "
                    f"{token!r}, which this site's {settings} "
                    f"{'requires' if len(requested[token]) == 1 else 'require'}; "
                    "give that edge a runtime that provides it, or narrow the "
                    "deployment to edges that do"
                ),
            )


def _explanations(
    inputs: ReleaseInputs, sites: Sequence[CdnSite]
) -> tuple[HostExplanation, ...]:
    """Attribute each host's settings to its zone or rule; skip unrecoverable names."""
    zones = {zone.name: zone for zone in inputs.domains}
    rules = {(rule.domain, rule.name): rule for rule in inputs.rules}
    explanations: list[HostExplanation] = []
    for site in sites:
        source = host_source(list(inputs.domains), list(inputs.rules), site.name)
        if source is None:
            continue
        zone_name, rule_name = source
        explanations.append(
            explain(
                zones[zone_name],
                rules.get((zone_name, rule_name)) if rule_name else None,
                host=site.name,
                server_names=site.server_names,
                origin_host=site.origin_host,
            )
        )
    return tuple(explanations)
