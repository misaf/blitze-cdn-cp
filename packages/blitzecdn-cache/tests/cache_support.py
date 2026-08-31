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
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn_cache.domain import PurgeEntry


def purges(fake: object) -> list[tuple[tuple[PurgeEntry, ...], bool, str | None]]:
    """Every cache purge the fleet was asked to run, as this capability meant it."""
    recorded: Sequence[tuple[str, object, dict[str, object], str | None]] = (
        fake.playbooks  # type: ignore[attr-defined]
    )
    return [
        (
            tuple(
                PurgeEntry.model_validate(entry)
                for entry in variables["blitzecdn_cache_purge_entries"]  # type: ignore[union-attr]
            ),
            bool(variables["blitzecdn_cache_purge_all"]),
            limit,
        )
        for name, _playbook, variables, limit in recorded
        if name == "cache-purge"
    ]


def stats_limits(fake: object) -> list[str | None]:
    """The host limit each statistics run was asked for."""
    recorded: Sequence[tuple[str, object, dict[str, object], str | None]] = (
        fake.playbooks  # type: ignore[attr-defined]
    )
    return [limit for name, _playbook, _variables, limit in recorded if name == "stats"]
