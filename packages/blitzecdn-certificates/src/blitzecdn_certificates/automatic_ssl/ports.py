"""What the Automatic SSL/TLS scan needs from the outside world.

The consumer owns the port, so this is declared here rather than imported from
the distribution that satisfies it. ``blitzecdn-origins`` ships an adapter with
exactly this shape and this package declares that distribution as a real
dependency, but the *description* of what the scan calls belongs to the scan:
an implementation is a fact about wiring, and wiring is
:mod:`blitzecdn_certificates.composition`'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from blitzecdn.core.runs import AnsibleRun

__all__ = ["OriginCheckRunner"]


class OriginCheckRunner(Protocol):
    """Ask every edge in scope to connect to the origins it proxies to.

    The scan calls it twice — once for each site's current transport and once
    for the same sites under Full (strict) — and upgrades only where every
    edge answered and both answers agree.
    """

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...
