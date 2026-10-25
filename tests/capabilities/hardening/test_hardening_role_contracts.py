"""What this capability's roles promise the host, asserted where they live.

The other half of the SSH contract, `ansible/ansible.cfg` refusing anything
but a key, is core's and tested there. Each file here is written by one role
and removed by another, and the pair only works while they agree.

Read through :data:`blitzecdn.capabilities.hardening.ansible.ROLES_PATH`, the
path a deployment resolves the roles by.
"""

from __future__ import annotations

from typing import Any

import yaml

from blitzecdn.capabilities.hardening import ansible

SSHD = ansible.ROLES_PATH / "blitzecdn_hardening_sshd"
FAIL2BAN = ansible.ROLES_PATH / "blitzecdn_hardening_fail2ban"
TEARDOWN = ansible.ROLES_PATH / "blitzecdn_hardening_teardown"

#: The two files this capability puts on a host. Installing and enabling
#: Fail2Ban is package state the host keeps.
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

    If the drop-in were relaxed, deploys would keep working while every edge
    quietly accepted passwords again.
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

    Detaching the capability is explicit; flipping this default would leave it
    attached and hardening nothing.
    """
    defaults = _defaults(SSHD)

    assert defaults["blitzecdn_hardening_sshd_enabled"] is True
    assert defaults["blitzecdn_hardening_sshd_permit_root_login"] == "no"


def test_the_roles_own_their_fleet_policy() -> None:
    """Every setting these roles read is declared in their own defaults.

    Checked per role rather than against the union: the teardown role's options
    are deliberate copies of others', and it runs on a host the converging roles
    no longer touch, so the copies must be in its own defaults.
    """
    for role in (SSHD, FAIL2BAN, TEARDOWN):
        missing = set(_spec(role)) - set(_defaults(role))
        assert not missing, f"{role.name} declares {missing} with no default to supply"


def test_the_ssh_role_refuses_to_strand_an_account() -> None:
    """The pre-flight that makes this role safe to run against a live fleet.

    Disabling password authentication can lock an operator out. The proof runs on
    the host: authorized_keys present, non-empty, and passing sshd's StrictModes
    ownership and mode rules.
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

    The teardown role keeps its own copies so removal does not depend on the
    converging roles resolving, as `blitzecdn_edge_teardown` does. Drift would be
    silent: the decommission succeeds and the host stays key-only.
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

    Handlers flush at play end, after `blitzecdn_edge_teardown` has passed its
    verdict, so the host must be back on its own policy before then.
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

    Otherwise there is a window with no jail and password authentication back.
    """
    names = [task["name"] for task in _tasks(TEARDOWN)]

    assert names.index("Remove the managed Fail2Ban jail") < names.index(
        "Remove the managed SSH policy"
    )


def test_the_teardown_role_removes_both_whatever_the_fleet_now_says() -> None:
    """Unguarded by `blitzecdn_hardening_sshd_enabled`, deliberately.

    The decommissioning controller's configuration may have drifted; a guard
    would leave the files behind on hosts nothing can reach again.
    """
    for task in _tasks(TEARDOWN):
        assert "blitzecdn_hardening_sshd_enabled" not in str(task.get("when", ""))
