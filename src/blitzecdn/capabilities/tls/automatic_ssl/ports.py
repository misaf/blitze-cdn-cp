"""What the Automatic SSL/TLS scan needs from outside; ``origins`` satisfies it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["OriginCheckRunner"]


class OriginCheckRunner(Protocol):
    """Ask every edge in scope to connect to the origins it proxies to.

    Called for the current transport and again under Full (strict); an upgrade
    needs every edge answering both, consistently.
    """

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...
