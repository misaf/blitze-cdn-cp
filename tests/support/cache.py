"""Reading the generic fleet record back in this capability's own terms.

`FakeRunner` records `run_playbook(name, playbook, variables, limit)` and
nothing more. `CachePlaybooks` translates a purge into that document, so these
helpers translate it back. Asserting through the real adapter means a change
to the variables the purge role reads fails a test.
"""

from __future__ import annotations

from blitzecdn.capabilities.cache.domain import PurgeEntry, PurgePrefix
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


def purged_prefixes(fake: FakeRunner) -> list[tuple[PurgePrefix, ...]]:
    """The hostname and path prefixes of every cache purge the fleet ran."""
    purged = []
    for name, _playbook, variables, _limit in fake.playbooks:
        if name != "cache-purge":
            continue
        prefixes = variables["blitzecdn_cache_purge_prefixes"]
        assert isinstance(prefixes, list)
        purged.append(tuple(PurgePrefix.model_validate(prefix) for prefix in prefixes))
    return purged


def purged_tags(fake: FakeRunner) -> list[tuple[str, ...]]:
    """The tags of every cache purge the fleet ran."""
    purged = []
    for name, _playbook, variables, _limit in fake.playbooks:
        if name != "cache-purge":
            continue
        tags = variables["blitzecdn_cache_purge_tags"]
        assert isinstance(tags, list)
        purged.append(tuple(str(tag) for tag in tags))
    return purged
