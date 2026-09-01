"""Publish immutable snapshots in the format consumed by Ansible.

This renderer knows how a desired-state document is *framed* — a list of site
documents under one key, with fleet-wide variables beside it — and nothing at
all about what goes in one. Every variable comes from a plugin: `sites` projects
the site model, `certificates` replaces the two TLS paths with the files on this
controller, `http` states the fleet's baseline listener stance, and `http3`
overrides it with the QUIC requirement it derives — when that distribution is
installed at all.

That is the point. Adding compression, a WAF, GeoIP or visitor headers used to
mean editing this file, which made it the one place every future capability had
to meet. Now it is the one place none of them do.
"""

from __future__ import annotations

from pathlib import Path

from blitzecdn.features.deployments.ports import StateContributors, YamlWriter
from blitzecdn.features.deployments.snapshots import decode_snapshot


class DesiredStateRenderer:
    def __init__(
        self,
        *,
        allow_empty_sites: bool,
        contributors: StateContributors,
        write_yaml: YamlWriter,
    ) -> None:
        #: Whether an edge may converge to serving nothing. Renderer policy
        #: rather than a plugin contribution: it is a statement about this
        #: document being complete, not about any capability in it.
        self.allow_empty_sites = allow_empty_sites
        self.contributors = contributors
        self.write_yaml = write_yaml

    def render(self, snapshot: str, path: Path) -> None:
        sites = tuple(decode_snapshot(snapshot))
        self.write_yaml(
            path,
            {
                **self.contributors.fleet_variables(sites),
                "blitzecdn_nginx_allow_empty_sites": self.allow_empty_sites,
                "blitzecdn_nginx_sites": [
                    self.contributors.site_variables(site) for site in sites
                ],
            },
        )
