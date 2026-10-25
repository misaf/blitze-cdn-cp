"""The two plays this capability runs, and the documents they are given."""

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

    The plays ship with this capability; core is given only their paths.
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
