"""The pinned Ansible collections a real playbook run resolves modules from.

A role-contract test that runs ``ansible-playbook`` without them fails deep in
Ansible as ``couldn't resolve module/action 'ansible.posix.sysctl'``, which
reads as a broken role. Asking first turns that into one line naming the fix.

The question is ``scripts/collections-check.sh``'s, the one ``just
ansible-check`` and ``just doctor`` ask, so the three cannot disagree about what
"installed" means.
"""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

import pytest

from support.paths import REPO_ROOT

#: Where ``just collections`` installs them, relative to the checkout.
COLLECTIONS_PATH = REPO_ROOT / ".state" / "collections"

_CHECK = REPO_ROOT / "scripts" / "collections-check.sh"


@cache
def _missing() -> tuple[str, ...]:
    result = subprocess.run(  # noqa: S603 - fixed script in this checkout
        ["bash", str(_CHECK)],  # noqa: S607 - bash from PATH, as `just` runs it
        capture_output=True,
        text=True,
        check=False,
    )
    missing = tuple(
        line.rsplit(" ", 1)[0]
        for line in result.stdout.splitlines()
        if line.endswith(" missing")
    )
    if result.returncode not in (0, 1) or (result.returncode == 1 and not missing):
        pytest.fail(
            f"{_CHECK.name} exited {result.returncode} without a verdict:\n"
            f"{result.stdout}{result.stderr}",
            pytrace=False,
        )
    return missing


def pinned_collections() -> Path:
    """``ANSIBLE_COLLECTIONS_PATH`` for a playbook run, failing if a pin is absent.

    Fails rather than skips: CI installs the collections, so a skip there would
    hide a contract that never ran.
    """
    missing = _missing()
    if missing:
        pytest.fail(
            f"pinned Ansible collections are not installed in {COLLECTIONS_PATH} "
            f"({', '.join(missing)}); run `just collections` in this checkout",
            pytrace=False,
        )
    return COLLECTIONS_PATH
