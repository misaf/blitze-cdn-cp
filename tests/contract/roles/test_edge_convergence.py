"""Converging the shipped contract against a real playbook run."""

# ruff: noqa: F403,F405

from contract_support import *
from role_contract_support import (
    _defaults_of,
)


def _run_contract(
    tmp_path: Path, contract: dict[str, Any] | None = None, **inputs: Any
):
    """Execute the contract role, which is where its invariants live.

    Reading the assertions as data would prove they are written, not that they
    fire: a `when:` or an expression that raises at run time passes
    --syntax-check and ansible-lint alike.
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
    return subprocess.run(
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
            # The role carries the fleet's Nginx reload handler now, and the
            # handler runs `community.docker` modules. Nothing here notifies
            # it, but Ansible resolves a role's handlers when it loads the
            # role, so the collection has to be reachable or the play fails
            # before an assertion is reached.
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

    Which is why they are assertions rather than spec entries: the argument
    spec checks shape, and these are relationships between values.
    """
    contract = _runtime_defaults()["blitzecdn_edge_runtime"]
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

    A listener with no rule is an unreachable port and a rule with no listener
    is an open port that can never serve. Both roles read one contract member
    now, so the failure this guards is a rendering mistake rather than a
    disagreement — the port list reaching ufw has to be the contract's, in full,
    with UDP/443 present exactly when HTTP/3 is on.
    """
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")

    firewall = _role("blitzecdn_firewall")
    tasks = yaml.safe_load((firewall / "tasks/main.yml").read_text(encoding="utf-8"))
    compose = next(
        task
        for task in tasks[0]["block"]
        if task["name"] == "Compose the rule set this role manages"
    )

    def rules(http3: bool) -> set[str]:
        runtime = _runtime_defaults(blitzecdn_edge_http3_enabled=http3)
        computed = tmp_path / f"rules-{http3}.json"
        playbook = tmp_path / f"rules-{http3}.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": _defaults_of(firewall)
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

    listeners = _runtime_defaults()["blitzecdn_edge_runtime"]["listeners"]
    expected = {"tcp|22|198.51.100.0/24"} | {
        f"tcp|{port}|any" for port in listeners["http"] + listeners["https"]
    }

    assert rules(http3=False) == expected
    assert rules(http3=True) == expected | {"udp|443|any"}
