"""The two plays this capability runs, and the documents they are given.

What a :class:`PurgeEntry` looks like to Ansible is this capability's
knowledge. Core keeps only the generic primitive — run this play, with these
variables, against these hosts — because the shared Ansible adapter every
capability reaches through cannot know any capability's domain types.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.capabilities.cache import ansible
from blitzecdn.capabilities.cache.domain import PurgeEntry
from blitzecdn.capabilities.cache.ports import FleetPlaybooks
from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["CachePlaybooks", "purge_entry_to_ansible"]


def purge_entry_to_ansible(entry: PurgeEntry) -> dict[str, str]:
    """One cached response, as the purge role reads it."""
    return {"host": entry.host, "uri": entry.uri, "scheme": entry.scheme.value}


class CachePlaybooks:
    """``CacheRunner``, over the control plane's generic playbook primitive.

    The plays and the roles they name ship with this capability and are located
    through :mod:`blitzecdn.capabilities.cache.ansible`. Core is told the *path* —
    it stages
    the variables, expands the host limit against the fleet it records, and
    applies the timeout — and never what the play is for. Detaching this
    package therefore removes the Python that asks for a purge and the Ansible
    that carries one out together, which is what makes the capability whole.
    """

    def __init__(self, fleet: FleetPlaybooks) -> None:
        self._fleet = fleet

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun:
        return self._fleet.run_playbook(
            name="cache-purge",
            playbook=ansible.CACHE_PURGE_PLAYBOOK,
            variables={
                "blitzecdn_cache_purge_entries": [
                    purge_entry_to_ansible(entry) for entry in entries
                ],
                "blitzecdn_cache_purge_all": purge_all,
            },
            host_limit=host_limit,
        )

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun:
        return self._fleet.run_playbook(
            name="stats",
            playbook=ansible.STATS_PLAYBOOK,
            variables={},
            host_limit=host_limit,
        )
