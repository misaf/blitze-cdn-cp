"""The controller's own API rules: opened, withdrawn, and re-applied."""

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from support.contracts import PROJECT_DIR, jinja2, role_path
from support.role_contracts import (
    defaults_of,
)


def test_the_control_plane_admits_the_port_its_api_actually_listens_on():
    """A rule on a dead port and a listener behind a closed one are both silent.

    The role's default is held to the constant the listener uses.
    """
    from blitzecdn.entrypoints.api.__main__ import API_PORT

    defaults = defaults_of(role_path("blitzecdn_controlplane"))
    assert defaults["blitzecdn_controlplane_api_port"] == API_PORT


def test_the_control_plane_can_withdraw_an_api_rule_it_no_longer_manages():
    """Opening is the easy half; a removal has to be a removal.

    ufw keeps every rule it was given, so an address dropped from
    BLITZE_ALLOWED_IPS would still reach the host. Reading back what this role
    wrote closes that, as the edge firewall does for SSH sources.
    """
    tasks = (role_path("blitzecdn_controlplane") / "tasks/main.yml").read_text(
        encoding="utf-8"
    )

    assert "difference(blitzecdn_controlplane_desired_rules)" in tasks
    assert "delete: true" in tasks
    assert tasks.index("Allow the API from its allowed sources") < tasks.index(
        "Withdraw API rules this role no longer manages"
    )
    assert tasks.index("Withdraw API rules this role no longer manages") < tasks.index(
        "Record the managed API rule set"
    )
    # Never enables ufw or sets a policy: a controller-only host may be firewalled
    # by something else, and deny-by-default could cut off the running session.
    assert "state: enabled" not in tasks
    assert "policy: deny" not in tasks


def test_uninstalling_withdraws_the_api_rules_the_installation_added():
    """The registry lives in the directory the uninstall deletes.

    Withdrawing after that removal would leave port 8000 open with nothing left to
    name the rule. Both roles share one registry path rather than two literals.
    """
    control = role_path("blitzecdn_controlplane")
    uninstall = role_path("blitzecdn_uninstall")

    def registry(role: Path, prefix: str) -> str:
        defaults = defaults_of(role)
        return (
            jinja2.Template(defaults[f"{prefix}_firewall_registry_file"])
            .render(defaults)
            .strip()
        )

    assert registry(control, "blitzecdn_controlplane") == registry(
        uninstall, "blitzecdn_uninstall"
    )

    tasks = yaml.safe_load((uninstall / "tasks/main.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    withdraw = "Withdraw the API firewall rules this installation added"
    assert names.index(withdraw) < names.index(
        "Remove exact BlitzeCDN-owned host paths"
    )
    assert tasks[names.index(withdraw)]["community.general.ufw"]["delete"] is True

    # The rule shape the control plane records is the shape this reads back. The
    # allow lives inside a block, so the search descends into one.
    def flatten(entries: list[dict]) -> list[dict]:
        found = []
        for entry in entries:
            found.append(entry)
            found.extend(flatten(entry.get("block", [])))
        return found

    recorded = flatten(
        yaml.safe_load((control / "tasks/main.yml").read_text(encoding="utf-8"))
    )
    allow = next(
        task
        for task in recorded
        if task["name"] == "Allow the API from its allowed sources"
    )
    assert allow["community.general.ufw"]["src"] == "{{ item.split('|')[2] }}"


def test_the_control_plane_applies_a_given_access_list_on_every_run():
    """The environment file is written once; the flag is given on every run.

    Otherwise `--allowed-ips` would only seed a first installation. It lands
    before the services are recreated, since Compose reads the file then, and
    leaves the template's commented example alone.
    """
    role = role_path("blitzecdn_controlplane")
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks]
    setter = tasks[names.index("Set the API access list this run was given")]

    assert names.index("Set the API access list this run was given") < names.index(
        "Recreate and start the control-plane services"
    )
    assert "blitzecdn_controlplane_allowed_ips | length > 0" in setter["when"]
    assert (
        re.search(
            setter["ansible.builtin.lineinfile"]["regexp"],
            "# BLITZE_ALLOWED_IPS=203.0.113.8/32",
        )
        is None
    )
    assert (
        re.search(
            setter["ansible.builtin.lineinfile"]["regexp"],
            "BLITZE_ALLOWED_IPS=203.0.113.8/32",
        )
        is not None
    )
    # The default makes omitting the flag mean "leave the file alone", so a routine
    # `update` cannot cut off access configured by hand.
    assert defaults_of(role)["blitzecdn_controlplane_allowed_ips"] == ""


def test_the_control_plane_opens_exactly_the_allowed_sources(tmp_path):
    """Executed, not read: this is the rule set ufw is actually handed.

    Sources are parsed from the managed environment file the API reads. The
    commented example must not become a rule, nor a quoted value a source with a
    quote in it.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    role = role_path("blitzecdn_controlplane")
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text(encoding="utf-8"))
    compose = next(
        task
        for task in tasks
        if task["name"] == "Compose the API rules this role manages"
    )

    def rules(environment: str, index: int) -> set[str]:
        computed = tmp_path / f"api-rules-{index}.json"
        playbook = tmp_path / f"api-rules-{index}.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": defaults_of(role)
                        | {
                            "blitzecdn_controlplane_environment_content": {
                                "content": base64.b64encode(
                                    environment.encode("utf-8")
                                ).decode("ascii")
                            }
                        },
                        "tasks": [
                            compose,
                            {
                                "copy": {
                                    "content": (
                                        "{{ blitzecdn_controlplane_desired_rules "
                                        "| to_json }}"
                                    ),
                                    "dest": str(computed),
                                    "mode": "0600",
                                }
                            },
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
            [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("COV_CORE", "COVERAGE"))
            }
            | {"ANSIBLE_LOCALHOST_WARNING": "False"},
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return set(yaml.safe_load(computed.read_text(encoding="utf-8")))

    def render(allowed_ips: str) -> str:
        return jinja2.Template(
            (role / "templates/blitzecdn.env.j2").read_text(encoding="utf-8"),
            trim_blocks=True,
        ).render(
            blitzecdn_controlplane_api_key="secret",
            blitzecdn_controlplane_allowed_ips=allowed_ips,
        )

    shipped = render("")
    # The template as installed: the allowlist is only a comment.
    assert rules(shipped, 0) == set()

    assert rules("BLITZE_ALLOWED_IPS=203.0.113.8/32, 198.51.100.0/24\n", 1) == {
        "tcp|8000|203.0.113.8/32",
        "tcp|8000|198.51.100.0/24",
    }
    # dotenv accepts quotes, so an operator who writes them must not get a rule
    # for a source that begins with one.
    assert rules('BLITZE_ALLOWED_IPS="203.0.113.8/32"\n', 2) == {
        "tcp|8000|203.0.113.8/32"
    }
    # Emptied to return the API to loopback. Every rule is then stale, which is
    # what makes the withdrawal above revoke them.
    assert rules("BLITZE_ALLOWED_IPS=\n", 3) == set()

    # What `install.sh --allowed-ips` seeds is read back by the same parser, so the
    # flag, the API's list and ufw's ports are one fact.
    assert rules(render("203.0.113.8/32,198.51.100.0/24"), 4) == {
        "tcp|8000|203.0.113.8/32",
        "tcp|8000|198.51.100.0/24",
    }
