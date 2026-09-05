"""What this capability's roles promise the host, asserted where they live.

These moved here with the roles. The control plane still has the other half of
the SSH contract — `ansible/ansible.cfg` refusing to dial out with anything but
a key — and that assertion stayed in core's suite, because that file is core's.

Two of them are newer than the rest: each of this capability's files is now
written in one role and removed in another, and the pair only works while they
agree. Core used to hold the second copy and could not be asked to keep
agreeing with a wheel it may not have installed.

Read through :data:`blitzecdn_hardening.ansible.ROLES_PATH`, the same path a
deployment resolves the roles by, rather than a directory in the checkout that
an installed wheel would not have.
"""

from __future__ import annotations

from typing import Any

import yaml
from blitzecdn_hardening import ansible

SSHD = ansible.ROLES_PATH / "blitzecdn_hardening_sshd"
FAIL2BAN = ansible.ROLES_PATH / "blitzecdn_hardening_fail2ban"
TEARDOWN = ansible.ROLES_PATH / "blitzecdn_hardening_teardown"

#: The two files this capability puts on a host, and the whole of what it puts
#: there. Everything else it does — installing Fail2Ban, enabling its unit — is
#: package state the host keeps, not BlitzeCDN configuration to withdraw.
SSHD_DROP_IN = "/etc/ssh/sshd_config.d/50-blitzecdn.conf"
FAIL2BAN_JAIL = "/etc/fail2ban/jail.d/blitzecdn-sshd.local"


def _defaults(role: Any) -> dict[str, Any]:
    return yaml.safe_load((role / "defaults/main.yml").read_text(encoding="utf-8"))


def _tasks(role: Any) -> list[dict[str, Any]]:
    """Every task in a role's main file, blocks flattened."""
    loaded = yaml.safe_load((role / "tasks/main.yml").read_text(encoding="utf-8"))
    tasks: list[dict[str, Any]] = []
    for task in loaded:
        tasks.append(task)
        tasks.extend(task.get("block", []))
    return tasks


def test_the_role_enforces_public_key_only_ssh() -> None:
    """The control plane reaches every edge over SSH and nothing else.

    `ansible/ansible.cfg` refuses to authenticate with anything but a key, and
    this role is what makes the hosts agree. If a future edit relaxes this
    drop-in, deploys keep working — the controller still has its key — while
    every edge quietly starts accepting passwords again. Nothing else in either
    repository would notice.
    """
    template = (SSHD / "templates/sshd.conf.j2").read_text(encoding="utf-8")
    directives = {
        line.split()[0].lower(): line.split(maxsplit=1)[1].strip()
        for line in template.splitlines()
        if line and not line.startswith(("#", "{"))
    }
    for keyword, expected in (
        ("pubkeyauthentication", "yes"),
        ("authenticationmethods", "publickey"),
        ("passwordauthentication", "no"),
        ("kbdinteractiveauthentication", "no"),
        ("permitemptypasswords", "no"),
        ("hostbasedauthentication", "no"),
    ):
        assert directives.get(keyword) == expected, (
            f"blitzecdn_hardening_sshd no longer sets {keyword} {expected}. Edges "
            "would accept something other than public keys."
        )


def test_ssh_hardening_is_on_by_default() -> None:
    """Opting out is possible; arriving opted out by accident is not.

    Two ways to opt out now, and only one of them is silent. Detaching the
    distribution is explicit and fleet-wide; flipping this default would leave
    the capability attached, reported as installed by `blitzecdn plugins`, and
    hardening nothing.
    """
    defaults = _defaults(SSHD)

    assert defaults["blitzecdn_hardening_sshd_enabled"] is True
    assert defaults["blitzecdn_hardening_sshd_permit_root_login"] == "no"


def test_the_roles_own_their_fleet_policy() -> None:
    """Every setting these roles read is declared in their own defaults.

    They used to be split: three keys sat in the control plane's shipped
    `group_vars`, where an operator who had detached this package would still
    find a file describing a role no edge runs. A default that lives anywhere
    but here is a default this wheel cannot carry with it.

    Per role rather than against the union of all three. The teardown role's
    four options are copies of two of the others' by design, and a union would
    accept the copies going missing from its own defaults — which is the one
    place they have to be, since it runs on a host the converging roles are no
    longer touching.
    """
    for role in (SSHD, FAIL2BAN, TEARDOWN):
        missing = set(_spec(role)) - set(_defaults(role))
        assert not missing, f"{role.name} declares {missing} with no default to supply"


def test_the_ssh_role_refuses_to_strand_an_account() -> None:
    """The pre-flight that makes this role safe to run against a live fleet.

    Disabling password authentication is the one change in this workspace that
    can permanently lock an operator out of an edge. The proof has to happen on
    the host — authorized_keys present, non-empty, and passing the ownership
    and mode rules sshd applies under StrictModes — because a controller-side
    check would be reasoning about a file it cannot see.
    """
    tasks = (SSHD / "tasks/main.yml").read_text(encoding="utf-8")

    assert "Refuse to disable passwords without a working key" in tasks
    assert "StrictModes" in tasks
    # And the drop-in is written only after those assertions, never before.
    assert tasks.index(
        "Refuse to disable passwords without a working key"
    ) < tasks.index("src: sshd.conf.j2")


def _spec(role: Any) -> dict[str, Any]:
    document = yaml.safe_load(
        (role / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


# --- withdrawal -------------------------------------------------------------


def test_the_roles_that_write_these_files_and_the_role_that_removes_them_agree() -> (
    None
):
    """Two paths, each spelled in two roles, and nothing else checks the pairs.

    The teardown role carries its own copies on purpose — it runs on a host that
    is no longer being converged, whose inventory entry is about to be deleted,
    so reading the converging roles' defaults would make the removal depend on
    those roles still resolving. That is the same reason core's
    `blitzecdn_teardown` carries copies of the paths *it* removes. The cost of a
    copy is that it can drift, and drift here is silent: the decommission
    reports success and the host stays public-key-only against a control plane
    that has forgotten it exists.
    """
    jail = next(
        task["ansible.builtin.template"]["dest"]
        for task in _tasks(FAIL2BAN)
        if "ansible.builtin.template" in task
    )
    teardown = _defaults(TEARDOWN)

    assert _defaults(SSHD)["blitzecdn_hardening_sshd_config_path"] == SSHD_DROP_IN
    assert jail == FAIL2BAN_JAIL
    assert teardown["blitzecdn_hardening_teardown_sshd_file"] == SSHD_DROP_IN
    assert teardown["blitzecdn_hardening_teardown_fail2ban_file"] == FAIL2BAN_JAIL
    assert (
        teardown["blitzecdn_hardening_teardown_sshd_service"]
        == _defaults(SSHD)["blitzecdn_hardening_sshd_service"]
    )


def test_the_teardown_role_settles_both_services_before_the_verdict() -> None:
    """It reloads and restarts inline rather than notifying handlers.

    Handlers flush at the end of the play, which in the decommission play is
    after `blitzecdn_teardown` has already asserted the host is clean and passed
    the verdict on the whole run. A host about to leave inventory has to be back
    on its own access policy *before* then, and this role has to be the thing
    that fails the decommission if it is not.
    """
    tasks = _tasks(TEARDOWN)
    names = [task["name"] for task in tasks]

    assert not (TEARDOWN / "handlers").exists()
    assert any("ansible.builtin.assert" in task for task in tasks)
    # sshd -t before the reload, exactly as the converging role's handler does.
    assert names.index("Validate SSH configuration without the managed policy") < (
        names.index("Reload SSH without the managed policy")
    )


def test_the_jail_is_withdrawn_before_the_policy() -> None:
    """The reverse of the order the host slot converged them.

    Converging goes SSH then Fail2Ban so the jail protects a daemon that has
    already stopped accepting passwords. Withdrawing in the same order would
    leave a window in which the jail is gone and password authentication is
    back — brief, but on a host whose whole reason for being hardened was that
    it is reachable from the internet.
    """
    names = [task["name"] for task in _tasks(TEARDOWN)]

    assert names.index("Remove the managed Fail2Ban jail") < names.index(
        "Remove the managed SSH policy"
    )


def test_the_teardown_role_removes_both_whatever_the_fleet_now_says() -> None:
    """Unguarded by `blitzecdn_hardening_sshd_enabled`, deliberately.

    A host is usually decommissioned by a controller whose configuration has
    drifted from the one that converged it. A fleet that hardened its hosts once
    and turned the role off later would, with a guard here, leave both files
    behind on every host it ever decommissioned — and nothing could reach those
    hosts again to notice.
    """
    for task in _tasks(TEARDOWN):
        assert "blitzecdn_hardening_sshd_enabled" not in str(task.get("when", ""))
