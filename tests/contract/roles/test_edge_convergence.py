"""Converging the shipped contract against a real playbook run."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from support.contracts import PROJECT_DIR, ROLES_DIR, role_path, runtime_defaults
from support.role_contracts import (
    defaults_of,
)


def _run_contract(
    tmp_path: Path, contract: dict[str, Any] | None = None, **inputs: Any
):
    """Execute the contract role, which is where its invariants live.

    A `when:` that raises at run time passes --syntax-check and ansible-lint
    alike.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    variables: dict[str, Any] = dict(inputs)
    if contract is not None:
        variables["blitzecdn_edge_runtime"] = contract
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir(exist_ok=True)
    playbook = tmp_path / "contract.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": variables,
                    "roles": ["blitzecdn_edge"],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
            "ANSIBLE_ROLES_PATH": str(ROLES_DIR),
            # The role carries the Nginx reload handler, which uses `community.docker`.
            # Ansible resolves handlers when it loads the role, so the collection must
            # be reachable.
            "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
        },
        check=False,
    )


def test_the_shipped_contract_converges(tmp_path):
    """The defaults this collection ships have to pass their own validation."""
    result = _run_contract(tmp_path)
    assert result.returncode == 0, result.stdout


def test_the_shipped_contract_converges_with_http3_on(tmp_path):
    """HTTP/3 is the one input desired state writes on every deploy."""
    result = _run_contract(tmp_path, blitzecdn_edge_http3_enabled=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            {"listeners": {"http": [], "https": [443], "http3": False}},
            "no HTTP listeners",
            id="no-http-listeners",
        ),
        pytest.param(
            {"listeners": {"http": [80], "https": [], "http3": False}},
            "no HTTPS listeners",
            id="no-https-listeners",
        ),
        pytest.param(
            {"listeners": {"http": [80, 8443], "https": [443, 8443], "http3": False}},
            "both the HTTP and the HTTPS listener set",
            id="port-in-both-sets",
        ),
        pytest.param(
            {"listeners": {"http": [80], "https": [8443], "http3": True}},
            "443 is not an HTTPS listener",
            id="http3-without-443",
        ),
        pytest.param(
            {"status": {"address": "127.0.0.1", "port": 443, "path": "/stub_status"}},
            "not a usable loopback endpoint",
            id="status-on-a-public-listener",
        ),
        pytest.param(
            {"status": {"address": "127.0.0.1", "port": 8090, "path": "stub_status"}},
            "not a usable loopback endpoint",
            id="status-path-without-a-slash",
        ),
        pytest.param(
            {"paths": {"nginx": "etc/nginx"}},
            "is not an absolute path",
            id="relative-path",
        ),
    ],
)
def test_the_contract_refuses_an_edge_that_could_not_serve(tmp_path, change, message):
    """Each of these is well-typed, passes the argument spec, and cannot serve.

    The spec checks shape; these are relationships between values.
    """
    contract = runtime_defaults()["blitzecdn_edge_runtime"]
    contract = {
        key: (value | change[key] if key in change else value)
        if isinstance(value, dict)
        else value
        for key, value in contract.items()
    }

    result = _run_contract(tmp_path, contract)

    assert result.returncode != 0, result.stdout
    assert message in result.stdout, result.stdout


def test_the_firewall_opens_exactly_the_listeners_the_contract_declares(tmp_path):
    """Executed, not read: this is the rule set ufw is actually handed.

    The port list reaching ufw is the contract's in full, with UDP/443 present
    exactly when HTTP/3 is on.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    firewall = role_path("blitzecdn_firewall")
    tasks = yaml.safe_load((firewall / "tasks/main.yml").read_text(encoding="utf-8"))
    compose = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Compose the rule set this role manages"
    )

    def rules(http3: bool) -> set[str]:
        runtime = runtime_defaults(blitzecdn_edge_http3_enabled=http3)
        computed = tmp_path / f"rules-{http3}.json"
        playbook = tmp_path / f"rules-{http3}.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": defaults_of(firewall)
                        | {
                            "blitzecdn_edge_runtime": runtime["blitzecdn_edge_runtime"],
                            "blitzecdn_firewall_ssh_port": 22,
                            "blitzecdn_firewall_ssh_sources": ["198.51.100.0/24"],
                        },
                        "tasks": [
                            compose,
                            {
                                "copy": {
                                    "content": (
                                        "{{ blitzecdn_firewall_desired_rules "
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

    listeners = runtime_defaults()["blitzecdn_edge_runtime"]["listeners"]
    expected = {"tcp|22|198.51.100.0/24"} | {
        f"tcp|{port}|any" for port in listeners["http"] + listeners["https"]
    }

    assert rules(http3=False) == expected
    assert rules(http3=True) == expected | {"udp|443|any"}


def _controller_source_check(
    tmp_path: Path, ssh_connection: str | None, sources: list[str], **inputs: Any
) -> subprocess.CompletedProcess[str]:
    """Run the role's controller-source guard as shipped, minus the local skip.

    The run is local, so SSH_CONNECTION comes from the play's environment rather
    than a real session, and the `ansible_connection` clause that skips local
    runs is dropped so the guard still fires.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    firewall = role_path("blitzecdn_firewall")
    tasks = yaml.safe_load((firewall / "tasks/main.yml").read_text(encoding="utf-8"))
    guard = [
        task | {"when": [c for c in task["when"] if "ansible_connection" not in c]}
        for task in tasks[0]["block"]
        if task["name"]
        in {
            "Read the address the controller connects from",
            "Refuse SSH sources that would lock the controller out",
        }
    ]
    assert len(guard) == 2
    playbook = tmp_path / "source-check.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "environment": (
                        {}
                        if ssh_connection is None
                        else {"SSH_CONNECTION": ssh_connection}
                    ),
                    "vars": defaults_of(firewall)
                    | {"blitzecdn_firewall_ssh_sources": sources}
                    | inputs,
                    "tasks": guard,
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 - fixed ansible-playbook argv over tmp_path
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE", "SSH_CONNECTION"))
        }
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_FILTER_PLUGINS": str(firewall / "filter_plugins"),
        },
        check=False,
    )


@pytest.mark.parametrize(
    ("connection", "sources"),
    [
        ("198.51.100.7 51022 203.0.113.10 22", ["192.0.2.0/24", "198.51.100.0/24"]),
        ("198.51.100.7 51022 203.0.113.10 22", ["198.51.100.7"]),
        ("::ffff:198.51.100.7 51022 ::ffff:203.0.113.10 22", ["198.51.100.0/24"]),
        ("2001:db8:1::5 51022 2001:db8::10 22", ["2001:db8:1::/48"]),
    ],
)
def test_the_firewall_admits_a_controller_inside_its_ssh_sources(
    tmp_path, connection, sources
):
    result = _controller_source_check(tmp_path, connection, sources)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_the_firewall_refuses_ssh_sources_that_leave_the_controller_out(tmp_path):
    """The established session would survive; the next run would not connect.

    Refused before ufw is touched, naming the address and the networks.
    """
    result = _controller_source_check(
        tmp_path, "198.51.100.7 51022 203.0.113.10 22", ["192.0.2.0/24"]
    )
    assert result.returncode != 0, result.stdout
    assert "198.51.100.7" in result.stdout, result.stdout
    assert "192.0.2.0/24" in result.stdout, result.stdout


def test_the_firewall_refuses_when_it_cannot_tell_the_controller_address(tmp_path):
    result = _controller_source_check(tmp_path, None, ["192.0.2.0/24"])
    assert result.returncode != 0, result.stdout
    assert "SSH_CONNECTION is not set" in result.stdout, result.stdout


def test_the_controller_source_check_can_be_turned_off(tmp_path):
    result = _controller_source_check(
        tmp_path,
        "198.51.100.7 51022 203.0.113.10 22",
        ["192.0.2.0/24"],
        blitzecdn_firewall_verify_controller_source=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_the_controller_source_check_runs_before_the_firewall_changes():
    firewall = role_path("blitzecdn_firewall")
    tasks = yaml.safe_load((firewall / "tasks/main.yml").read_text(encoding="utf-8"))
    names = [task["name"] for task in tasks[0]["block"]]
    guard = names.index("Refuse SSH sources that would lock the controller out")
    assert guard < names.index("Install UFW")
    assert guard < names.index("Allow SSH from trusted management networks")
