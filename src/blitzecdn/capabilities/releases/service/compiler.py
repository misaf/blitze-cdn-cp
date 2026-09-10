"""Compile canonical state into the release the fleet is converged from.

One function, and the constraint that shapes it is what it may *not* do. It
reads no database, opens no socket, writes no file and looks at no clock. Every
input arrives as an argument, so a compilation is reproducible by construction:
run it twice on the same inputs and the digests match, run it in a test and it
needs nothing running.

That is worth stating because the code this replaces did none of it. Desired
state was serialised when a deployment was queued, plugin variables were merged
when the run reached the renderer, and capability validation happened somewhere
between the two against whatever was installed at that moment. Three readings of
"what should the fleet serve", none of them recorded, and nothing that could
answer the question after the run had finished.

Findings rather than exceptions
-------------------------------
A release that cannot be converged is still compiled, and carries the reasons.
``blitzecdn validate`` and ``blitzecdn deploy`` ask the same question, and an
operator fixing a fleet wants every problem in it — a compiler that raised on
the first would hand them one hostname per run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from blitzecdn.capabilities.dns.domain import CdnSite, derive_hosts, unservable_hosts

# `host_source` is reached through its own module rather than the package
# façade. It inverts the derived-host naming rule, which is a question only
# something reconstructing a site's provenance asks — this compiler and
# nothing else so far — and the façade is the surface every capability and
# every wheel binds to.
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.releases.domain import (
    COMPILER_VERSION,
    DESIRED_STATE_ARTIFACT,
    Artifact,
    CompiledSite,
    EdgeCapabilities,
    HostExplanation,
    Release,
    ReleaseFinding,
    ReleaseInputs,
    explain,
)
from blitzecdn.capabilities.releases.ports import SiteChecks, StateContributors

__all__ = ["compile_release"]


def compile_release(
    inputs: ReleaseInputs,
    *,
    capabilities: Iterable[str],
    targets: Sequence[EdgeCapabilities],
    contributors: StateContributors,
    site_checks: SiteChecks,
    allow_empty_sites: bool,
) -> Release:
    """Derive, validate, render and explain — in that order, once.

    ``capabilities`` is what this installation has installed;  ``targets`` is
    what the edges the run is aimed at are declared to provide. Both are inputs
    to the digest, because both change the answer: a site's Brotli settings
    contribute nothing to the artifact with the compression wheel detached, and
    the same state is servable by one edge's runtime and not another's.

    ``site_checks`` is how installed capabilities object to a site for reasons
    the compiler cannot know — a WAF rule set that will not parse, an origin
    that resolves nowhere. It is a function rather than the plugin registry so
    this stays a function of its arguments; the composition root binds the
    registry to it.
    """
    installed = tuple(sorted(set(capabilities)))
    ordered_targets = tuple(sorted(targets, key=lambda target: target.name))

    findings = list(_derivation_findings(inputs))
    # Only what an edge is actually asked to serve is compiled. A site with no
    # proxied hostnames is desired state — a rollback must bring it back — but
    # it is not yet an instruction to any edge, and rendering one would emit a
    # `server` block with an empty `server_name`, which nginx reads as the
    # default server for the listener.
    sites = tuple(
        site
        for site in derive_hosts(
            list(inputs.domains), list(inputs.rules), list(inputs.records)
        )
        if site.serves_traffic
    )

    # Two kinds of finding, and only one of them stops the artifact being
    # rendered. See `_objections` below.
    objections: list[ReleaseFinding] = []
    for site in sites:
        findings.extend(_capability_findings(site, installed, ordered_targets))
        objections.extend(site_checks(site))
    findings.extend(objections)

    # An installed capability that has *objected to a site* must not then be
    # asked to render it. That is not squeamishness: a capability contributes
    # its variables by being asked, and the objection is usually the reason
    # asking would fail. `blitzecdn-certificates` is the standing case — it
    # objects to a site in a controller-managed TLS mode whose material was
    # never issued, and asking it for that site's certificate paths raises a
    # `NotFoundError` about exactly the material it just objected about. The
    # operator would meet the exception instead of the objection.
    #
    # A *missing* capability is the other kind and does not stop rendering: the
    # wheel is not there to be asked, so it contributes nothing and the
    # document is simply the document without it. The release is still
    # unservable and nothing will converge it — but `blitzecdn release
    # artifact` can still show what this installation would send, which is the
    # question an operator asks while deciding what to install.
    if objections:
        return Release(
            compiler_version=COMPILER_VERSION,
            inputs_digest=inputs.digest,
            capabilities=installed,
            targets=ordered_targets,
            explanations=_explanations(inputs, sites),
            findings=tuple(findings),
        )

    compiled = tuple(
        CompiledSite(site=site, variables=dict(contributors.site_variables(site)))
        for site in sites
    )
    artifact = Artifact(
        name=DESIRED_STATE_ARTIFACT,
        document={
            **contributors.fleet_variables(sites),
            "blitzecdn_nginx_allow_empty_sites": allow_empty_sites,
            "blitzecdn_nginx_sites": [entry.variables for entry in compiled],
        },
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


def _derivation_findings(inputs: ReleaseInputs) -> Iterable[ReleaseFinding]:
    """Groups whose policy no longer composes into a virtual host.

    The derivation drops them rather than raising — one broken zone must not
    darken a whole fleet's worth of reads — so this is where the drop stops
    being silent. See ``docs/decisions/0005-canonical-writes-and-derived-state.md``.
    """
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
    """What this site asks for that either the controller or an edge lacks.

    Two checks, because they fail differently and are fixed differently. A
    capability missing from the *controller* means no wheel renders those
    settings, so the artifact silently omits them — the operator installs a
    distribution. A capability missing from an *edge* means the artifact is
    correct and that host cannot execute it — an nginx that never loaded the
    Brotli module reads a ``brotli on`` directive as a syntax error — so the
    operator rebuilds or replaces the edge, or aims the run elsewhere.

    An edge that has declared nothing is assumed to provide whatever the
    controller has. That is what every installation registered before edges
    could declare a set already behaves like, so the second check is opt-in and
    adding it changes no existing fleet's outcome.
    """
    requested = site.capability_requirements
    for token in sorted(set(requested) - set(installed)):
        settings = ", ".join(requested[token])
        yield ReleaseFinding(
            source="capabilities",
            host=site.name,
            message=(
                f"capability {token!r} is not installed, and this site's "
                f"{settings} {'requests' if len(requested[token]) == 1 else 'request'}"
                " it; install a distribution that provides it or disable the "
                "setting that requests it"
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
    """Attribute each compiled host's settings to the zone or rule behind it.

    A host whose source cannot be recovered is skipped rather than guessed at.
    ``host_source`` inverts the naming rule by asking it, so it answers ``None``
    only for a name no zone and rule in these inputs could have produced — which
    means the explanation would be about a different installation's state.
    """
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
