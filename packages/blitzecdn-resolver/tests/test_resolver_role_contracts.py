"""What this capability's roles promise the host, asserted where they live.

These moved here with the role, and one of them is new: the drop-in path is
written in two places now — the role that creates it and the role that removes
it — and the pair only works while they agree. Core used to hold the second
copy and could not be asked to keep agreeing with a wheel it may not have
installed.

Read through :data:`blitzecdn_resolver.ansible.ROLES_PATH`, the same path a
deployment resolves the roles by, rather than a directory in the checkout that
an installed wheel would not have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from blitzecdn_resolver import ansible

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

    The teardown role carries its own copy on purpose — it runs on a host that
    is no longer being converged, whose inventory entry is about to be deleted,
    so reading the other role's default would make the removal depend on the
    converging role's variables still resolving. That is the same reason core's
    `blitzecdn_teardown` carries copies of the paths *it* removes. The cost of
    a copy is that it can drift, and drift here is silent: the decommission
    reports success and the host keeps resolving through BlitzeCDN's servers
    forever.
    """
    written = next(
        task["ansible.builtin.template"]["dest"]
        for task in _tasks(RESOLVER)
        if "ansible.builtin.template" in task
    )

    assert written == DROP_IN
    assert _defaults(TEARDOWN)["blitzecdn_resolver_teardown_file"] == DROP_IN


def test_managing_resolution_is_off_until_a_fleet_asks_for_it() -> None:
    """Attaching the distribution must not change a single host on its own.

    Both halves of the default are load-bearing. The package is attached by
    default so that a controller upgraded in place keeps managing the hosts it
    already was; the role is disabled by default so that attaching it manages
    nobody who had not asked. Flipping this would silently replace the resolver
    on every edge in every fleet that merely took the upgrade.
    """
    assert _defaults(RESOLVER)["blitzecdn_resolver_enabled"] is False


def test_an_enabled_role_with_nothing_to_point_at_fails_closed() -> None:
    """The empty-list case is the one that resolves nothing at all.

    A drop-in with `Domains=~.` and no `DNS=` claims every domain and answers
    none of them, which takes the host's DNS away entirely rather than
    replacing it. Same shape as an empty firewall source list, and refused for
    the same reason.
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

    A resolver that returns an address for a reserved `.invalid` name invents
    answers, and every name-based check on that host — origin lookups, CAA
    preflight, the controller's own doctor probe — then compares against
    fiction while looking healthy. The probe runs after the handler flush, so
    what it queries is the resolver this role just installed.
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

    Handlers flush at the end of the play, which in the decommission play is
    after `blitzecdn_teardown` has already asserted the host is clean and
    passed the verdict on the whole run. A host that is about to leave
    inventory has to have its own resolver back *before* then, and this role
    has to be the thing that fails the decommission if it does not.
    """
    tasks = _tasks(TEARDOWN)

    assert not (TEARDOWN / "handlers/main.yml").exists()
    restart = next(task for task in tasks if "ansible.builtin.systemd_service" in task)
    assert restart["ansible.builtin.systemd_service"]["name"] == "systemd-resolved"
    assert restart["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert any("ansible.builtin.assert" in task for task in tasks)


def test_the_teardown_role_removes_the_drop_in_whatever_the_fleet_now_says() -> None:
    """Unguarded by `blitzecdn_resolver_enabled`, deliberately.

    A host is usually decommissioned by a controller whose configuration has
    drifted from the one that converged it. A fleet that enabled the role once
    and turned it off later would, with a guard here, leave the drop-in behind
    on every host it ever decommissioned — and nothing could reach those hosts
    again to notice.
    """
    for task in _tasks(TEARDOWN):
        assert "blitzecdn_resolver_enabled" not in str(task.get("when", ""))
