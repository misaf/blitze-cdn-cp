"""The two plays this capability runs, and the documents they are given.

This is the seam the extraction was about. ``purge_entry_to_ansible`` used to
live in ``blitzecdn.core.ansible.mapping`` and ``run_cache_purge`` on
``AnsibleRunner``, which meant the shared Ansible adapter — the one every
feature reaches through — knew what a :class:`PurgeEntry` was. Core cannot know
a detachable package's domain type, so the knowledge moved here and core kept
only the generic primitive: run this play, with these variables, against these
hosts.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.core.config import Settings
from blitzecdn.core.runs import AnsibleRun
from blitzecdn_cache.domain import PurgeEntry
from blitzecdn_cache.ports import FleetPlaybooks

__all__ = ["CachePlaybooks", "purge_entry_to_ansible"]


def purge_entry_to_ansible(entry: PurgeEntry) -> dict[str, str]:
    """One cached response, as the purge role reads it."""
    return {"host": entry.host, "uri": entry.uri, "scheme": entry.scheme.value}


class CachePlaybooks:
    """``CacheRunner``, over the control plane's generic playbook primitive.

    The playbooks themselves stay in the control plane's Ansible tree and are
    located through ``Settings``. Ansible remains the provisioning authority:
    detaching this package removes the Python that *asks* for a purge, never
    the role that would carry one out, and no desired state depends on either.
    """

    def __init__(self, settings: Settings, fleet: FleetPlaybooks) -> None:
        self._settings = settings
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
            playbook=self._settings.cache_purge_playbook_path,
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
            playbook=self._settings.stats_playbook_path,
            variables={},
            host_limit=host_limit,
        )
