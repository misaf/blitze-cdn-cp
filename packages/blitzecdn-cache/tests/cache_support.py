"""Reading the generic fleet record back in this capability's own terms.

The control plane's shared `FakeRunner` records `run_playbook(name, playbook,
variables, limit)` and nothing more, because that is all core's Ansible adapter
offers an installed distribution. Translating a purge into that document is
this package's job — `CachePlaybooks` does it — so translating it back is this
package's test helper, and neither belongs in the core `conftest`.

Asserting through here rather than against a hand-written `CacheRunner` double
is deliberate: it exercises the real adapter, so a change to the variable names
the purge role reads fails a test rather than passing one written against a
stub of ourselves.

`just types` checks this module. Both readers below used to take `fake: object`
and then reach for `.playbooks`, which is not a thing an `object` has — the
annotation was the widest one that would silence a reader rather than the type
the argument has, and nothing was in a position to say so. Naming `FakeRunner`
is what makes the record's shape come from the double's definition instead of
from a `Sequence[...]` alias restated here, which had already drifted: it
called the playbook `object` where the double records a `Path`.
"""

from __future__ import annotations

from blitzecdn_cache.domain import PurgeEntry
from control_plane_fixtures import FakeRunner


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
