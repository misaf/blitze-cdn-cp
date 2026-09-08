"""The controller's own API rules: opened, withdrawn, and re-applied."""

# ruff: noqa: F403,F405
import base64

from contract_support import *
from role_contract_support import (
    _defaults_of,
)


def test_the_control_plane_admits_the_port_its_api_actually_listens_on():
    """A rule on a dead port and a listener behind a closed one are both silent.

    The role opens ufw for the API, and the API chooses its own port. Two
    literals would disagree on the day one moved, and nothing would report it:
    the play would succeed, the container would be healthy, and the first
    symptom would be an allowed operator timing out. So the role's default is
    held to the constant the listener uses, the way the Dockerfile path is
    already held to `blitzecdn.docker.CONTROL_PLANE_DOCKERFILE`.
    """
    from blitzecdn.api.__main__ import API_PORT

    defaults = _defaults_of(_role("blitzecdn_controlplane"))
    assert defaults["blitzecdn_controlplane_api_port"] == API_PORT


def test_the_control_plane_can_withdraw_an_api_rule_it_no_longer_manages():
    """Opening is the easy half; a removal has to be a removal.

    ufw keeps every rule it has ever been given, so an address dropped from
    BLITZE_ALLOWED_IPS is denied by the application on the next recreate while
    the host goes on accepting its packets. Reading back what this role wrote
    is the only thing that closes that gap, and it is the same mechanism the
    edge firewall uses for its SSH sources.
    """
    tasks = (_role("blitzecdn_controlplane") / "tasks/main.yml").read_text(
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
    # Never enables ufw and never sets a policy: a controller-only host may be
    # firewalled by something else, and deny-by-default from a play it did not
    # ask for would cut off the session running it.
    assert "state: enabled" not in tasks
    assert "policy: deny" not in tasks


def test_uninstalling_withdraws_the_api_rules_the_installation_added():
    """The registry lives in the directory the uninstall deletes.

    Withdrawing after that removal, or not at all, leaves the host admitting
    port 8000 for an API that no longer exists and no file left on disk able to
    name those rules -- an opening no later run could close. The edge teardown
    already reads its own registry before removing its state; this is the same
    ordering for the control plane's.

    The two roles are held to one path rather than two literals, because a
    registry written to one file and read from another is a withdrawal that
    silently finds nothing to withdraw.
    """
    control = _role("blitzecdn_controlplane")
    uninstall = _role("blitzecdn_uninstall")

    def registry(role: Path, prefix: str) -> str:
        defaults = _defaults_of(role)
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

    Without this task `--allowed-ips` would seed a first installation and then
    silently do nothing, which is the worst possible shape for a flag that says
    who may reach the API. It has to land before the services are recreated,
    because Compose reads that file when it starts them, and it must not touch
    the commented example the template ships — a comment that became a rule
    would open a port nobody asked for.
    """
    role = _role("blitzecdn_controlplane")
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
    # The default is what makes omitting the flag mean "leave the file alone"
    # rather than "empty the list", so a routine `update` cannot cut off access
    # an operator configured by hand.
    assert _defaults_of(role)["blitzecdn_controlplane_allowed_ips"] == ""


def test_the_control_plane_opens_exactly_the_allowed_sources(tmp_path):
    """Executed, not read: this is the rule set ufw is actually handed.

    The sources are parsed out of the managed environment file rather than kept
    a second time in the inventory, because that file is what the API itself
    reads and what an operator edits. That makes the parsing the load-bearing
    part — the commented example the template ships must not become a rule, and
    a quoted value must not become a rule for a source with a quote in it.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    role = _role("blitzecdn_controlplane")
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
                        "vars": _defaults_of(role)
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
        result = subprocess.run(
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
    # The template as installed: the allowlist is present only as a comment,
    # and a commented example that opened a port would be the worst kind of
    # default.
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

    # And the other branch of the same template: what `install.sh --allowed-ips`
    # seeds is read back as rules by the same parser, so the flag, the list the
    # API enforces and the ports ufw opens are one fact rather than three that
    # agree today.
    assert rules(render("203.0.113.8/32,198.51.100.0/24"), 4) == {
        "tcp|8000|203.0.113.8/32",
        "tcp|8000|198.51.100.0/24",
    }
