"""What this capability's roles promise the host, asserted where they live.

These moved here with the roles. The control plane still has the other half of
the SSH contract — `ansible/ansible.cfg` refusing to dial out with anything but
a key — and that assertion stayed in core's suite, because that file is core's.

Read through :data:`blitzecdn_hardening.ansible.ROLES_PATH`, the same path a
deployment resolves the roles by, rather than a directory in the checkout that
an installed wheel would not have.
"""

from __future__ import annotations

from typing import Any

import yaml
from blitzecdn_hardening import ansible

SSHD = ansible.ROLES_PATH / "blitzecdn_sshd"
FAIL2BAN = ansible.ROLES_PATH / "blitzecdn_fail2ban"


def _defaults(role: Any) -> dict[str, Any]:
    return yaml.safe_load((role / "defaults/main.yml").read_text(encoding="utf-8"))


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
            f"blitzecdn_sshd no longer sets {keyword} {expected}. Edges "
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

    assert defaults["blitzecdn_sshd_enabled"] is True
    assert defaults["blitzecdn_sshd_permit_root_login"] == "no"


def test_the_roles_own_their_fleet_policy() -> None:
    """Every setting these roles read is declared in their own defaults.

    They used to be split: three keys sat in the control plane's shipped
    `group_vars`, where an operator who had detached this package would still
    find a file describing a role no edge runs. A default that lives anywhere
    but here is a default this wheel cannot carry with it.
    """
    sshd, fail2ban = _defaults(SSHD), _defaults(FAIL2BAN)
    declared = set(sshd) | set(fail2ban)

    for role, options in (
        (SSHD, _spec(SSHD)),
        (FAIL2BAN, _spec(FAIL2BAN)),
    ):
        missing = set(options) - declared
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
