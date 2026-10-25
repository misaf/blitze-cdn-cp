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

    Four structural insertion points — ``http``, server, access phase before
    dispatch, upstream location after it — rather than directives, so no
    capability can replace a whole server block.

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
    module_fragments: tuple[str, ...] = ()
