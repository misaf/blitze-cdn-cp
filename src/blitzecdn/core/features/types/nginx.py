"""Nginx template fragments a capability contributes to each release."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NginxContribution",
]


@dataclass(frozen=True, slots=True)
class NginxContribution:
    """Static Nginx template fragments shipped by a feature.

    Five structural insertion points — ``http``, server, access phase before
    dispatch, upstream location after it, and origin — rather than directives,
    so no capability can replace a whole server block.

    ``origin_fragments`` sit between the origin and the edge's cache: core
    routes a site's requests through a loopback hop whose location carries
    them, so a fragment there can rewrite the origin's response headers before
    the cache reads them (nginx's cache directives take no variables). A site
    for which every origin fragment renders empty gets no hop. They are
    rendered with ``listener_port`` and ``visitor_scheme`` set to the origin's
    port and scheme.

    ``module_fragments`` are not Nginx configuration but executable njs
    modules, rendered into the runtime's modules directory under their name
    without ``.j2``. They ship in the configuration release, so they must not
    carry a secret.
    """

    feature: str
    templates_path: Path
    http_fragments: tuple[str, ...] = ()
    server_fragments: tuple[str, ...] = ()
    access_fragments: tuple[str, ...] = ()
    upstream_fragments: tuple[str, ...] = ()
    origin_fragments: tuple[str, ...] = ()
    module_fragments: tuple[str, ...] = ()
