"""What this capability's roles promise the host, asserted where they live.

The drop-in path is written by one role and removed by another, and the pair
only works while they agree.

Read through :data:`blitzecdn.capabilities.resolver.ansible.ROLES_PATH`, the
path a deployment resolves the roles by.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from blitzecdn.capabilities.resolver import ansible

RESOLVER = ansible.ROLES_PATH / "blitzecdn_resolver"
TEARDOWN = ansible.ROLES_PATH / "blitzecdn_resolver_teardown"

#: The drop-in itself. systemd-resolved reads every *.conf in this directory,
#: so the name is what distinguishes ours from the image's and the operator's.
DROP_IN = "/etc/systemd/resolved.conf.d/blitzecdn.conf"


def _defaults(role: Path) -> dict[str, Any]:
    return yaml.safe_load((role / "defaults/main.yml").read_text(encoding="utf-8"))


def _tasks(role: Path) -> list[dict[str, Any]]:
    """Every task in a role's main file, blocks flattened."""
    loaded = yaml.safe_load((role / "tasks/main.yml").read_text(encoding="utf-8"))
    tasks: list[dict[str, Any]] = []
    for task in loaded:
        tasks.append(task)
        tasks.extend(task.get("block", []))
    return tasks


def test_the_role_that_writes_the_drop_in_and_the_role_that_removes_it_agree() -> None:
    """One path, spelled in two roles, and nothing else checks the pair.

    The teardown role keeps its own copy so removal does not depend on the
    converging role resolving. Drift would be silent: the host keeps BlitzeCDN's
    resolvers forever.
    """
    written = next(
        task["ansible.builtin.template"]["dest"]
        for task in _tasks(RESOLVER)
        if "ansible.builtin.template" in task
    )

    assert written == DROP_IN
    assert _defaults(TEARDOWN)["blitzecdn_resolver_teardown_file"] == DROP_IN


def test_managing_resolution_is_off_until_a_fleet_asks_for_it() -> None:
    """Attaching the capability must not change a single host on its own.

    The role is disabled by default, so only fleets that ask have their resolver
    replaced.
    """
    assert _defaults(RESOLVER)["blitzecdn_resolver_enabled"] is False


def test_an_enabled_role_with_nothing_to_point_at_fails_closed() -> None:
    """The empty-list case is the one that resolves nothing at all.

    `Domains=~.` with no `DNS=` claims every domain and answers none.
    """
    assertions = [
        task["ansible.builtin.assert"]
        for task in _tasks(RESOLVER)
        if "ansible.builtin.assert" in task
    ]

    assert any(
        "blitzecdn_resolver_addresses | length > 0" in assertion["that"]
        for assertion in assertions
    )


def test_the_role_proves_the_running_resolver_is_honest() -> None:
    """Writing the file is not the result; answering truthfully is.

    A resolver answering a reserved `.invalid` name invents answers, and every
    name-based check on the host then compares against fiction. The probe runs
    after the handler flush.
    """
    tasks = _tasks(RESOLVER)
    names = [task["name"] for task in tasks]

    assert any(task.get("ansible.builtin.meta") == "flush_handlers" for task in tasks)
    probe = next(
        task["ansible.builtin.command"]
        for task in tasks
        if "ansible.builtin.command" in task
    )
    assert probe["argv"][0] == "resolvectl"
    assert any(".invalid" in str(argument) for argument in probe["argv"])
    assert names.index("Apply resolution changes before verifying them") < names.index(
        "Probe the resolver for invented answers"
    )


def test_the_teardown_role_restores_the_host_before_the_verdict() -> None:
    """It restarts resolved inline rather than notifying a handler.

    Handlers flush after `blitzecdn_edge_teardown` has passed its verdict.
    """
    tasks = _tasks(TEARDOWN)

    assert not (TEARDOWN / "handlers/main.yml").exists()
    restart = next(task for task in tasks if "ansible.builtin.systemd_service" in task)
    assert restart["ansible.builtin.systemd_service"]["name"] == "systemd-resolved"
    assert restart["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert any("ansible.builtin.assert" in task for task in tasks)


def test_the_teardown_role_removes_the_drop_in_whatever_the_fleet_now_says() -> None:
    """Unguarded by `blitzecdn_resolver_enabled`, deliberately.

    The decommissioning controller's configuration may have drifted; a guard
    would leave the drop-in on hosts nothing can reach again.
    """
    for task in _tasks(TEARDOWN):
        assert "blitzecdn_resolver_enabled" not in str(task.get("when", ""))
