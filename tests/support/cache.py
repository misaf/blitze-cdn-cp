"""Reading the generic fleet record back in this capability's own terms.

`FakeRunner` records `run_playbook(name, playbook, variables, limit)` and
nothing more. `CachePlaybooks` translates a purge into that document, so these
helpers translate it back. Asserting through the real adapter means a change
to the variables the purge role reads fails a test.
"""

from __future__ import annotations

from blitzecdn.capabilities.cache.domain import PurgeEntry
from support.control_plane import FakeRunner


def purges(fake: FakeRunner) -> list[tuple[tuple[PurgeEntry, ...], bool, str | None]]:
    """Every cache purge the fleet was asked to run, as this capability meant it."""
    purged = []
    for name, _playbook, variables, limit in fake.playbooks:
        if name != "cache-purge":
            continue
        entries = variables["blitzecdn_cache_purge_entries"]
        assert isinstance(entries, list)
        purged.append(
            (
                tuple(PurgeEntry.model_validate(entry) for entry in entries),
                bool(variables["blitzecdn_cache_purge_all"]),
                limit,
            )
        )
    return purged


def stats_limits(fake: FakeRunner) -> list[str | None]:
    """The host limit each statistics run was asked for."""
    return [
        limit
        for name, _playbook, _variables, limit in fake.playbooks
        if name == "stats"
    ]
