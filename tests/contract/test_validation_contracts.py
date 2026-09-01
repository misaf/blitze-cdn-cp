# ruff: noqa: F403,F405
from contract_support import *

# ----------------------------------------------------------------------
# Executing the role's validation tasks
#
# Everything above reads the role as data or renders its templates. Neither
# evaluates a `when:` or an `assert`, and `--syntax-check` and ansible-lint do
# not either — a conditional that raises at run time passes all of them. That
# gap shipped a broken deploy once: `when: item.firewall is defined and
# item.firewall` parses, lints, and then fails on ansible-core 2.19+ because
# its result is a dict rather than a boolean.
#
# roles/blitzecdn_nginx/tasks/validate.yml holds every task that inspects
# desired state and refuses to proceed, and changes nothing on the host, so it
# can be run here against real model output.
# ----------------------------------------------------------------------

VALIDATE_TASKS = ROLE_DIR / "tasks/validate.yml"
NGINX_BUILD_INVARIANT_TASKS = ROLE_DIR / "tasks/build-invariant.yml"


def _run_validation(sites: list[dict[str, Any]], tmp_path: Path, **overrides: Any):
    """Execute the role's validation tasks against localhost."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    # A contract input is applied before the contract is composed; anything
    # else is an ordinary variable override on top of it.
    inputs, plain = _split_runtime(overrides)
    variables = (
        _role_defaults(**inputs)
        | {
            "blitzecdn_nginx_sites": sites,
        }
        | plain
    )
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir(exist_ok=True)
    playbook = tmp_path / "validate.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(VALIDATE_TASKS)}],
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
        # pytest-cov's subprocess hook would otherwise make the Ansible
        # child write statement-only coverage beside this run's branch data,
        # which coverage refuses to combine.
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        # Its own local temp: the ambient one is a single shared directory, and
        # these runs happen concurrently under xdist.
        | {
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_LOCAL_TEMP": str(ansible_local),
        },
        check=False,
    )


def _run_nginx_build_capability(tmp_path: Path, configure_arguments: str):
    """Execute the build invariant against a fabricated `nginx -V` banner.

    Nginx runs in a container now, so there is no binary on the host to fake.
    The invariant was split out of the probe for exactly this: the assertions
    read two variables, so a test can hand them the output of a build that does
    not exist rather than needing an engine, an image and a network.
    """
    ansible_local = tmp_path / "ansible-local"
    ansible_local.mkdir()
    playbook = tmp_path / "nginx-build-capability.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "blitzecdn_nginx_config_test_image": "example/edge:test",
                        "blitzecdn_nginx_build_status": 0,
                        "blitzecdn_nginx_build_output": (
                            "nginx version: nginx/1.27.0\n"
                            f"configure arguments: {configure_arguments}\n"
                        ),
                    },
                    "tasks": [{"import_tasks": str(NGINX_BUILD_INVARIANT_TASKS)}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            shutil.which("ansible-playbook")
            or str(PROJECT_DIR / ".venv/bin/ansible-playbook"),
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
        ],
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
        },
        check=False,
    )


def test_nginx_invariant_accepts_a_capable_build(tmp_path):
    result = _run_nginx_build_capability(
        tmp_path, "--with-http_v3_module --with-http_ssl_module"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nginx_invariant_rejects_an_unsupported_build_clearly(tmp_path):
    result = _run_nginx_build_capability(tmp_path, "--with-http_ssl_module")
    assert result.returncode != 0
    assert "--with-http_v3_module is required" in result.stdout
    assert "will not be silently disabled" in result.stdout


def test_role_validation_tasks_actually_run(desired_state, tmp_path):
    """The regression guard for a conditional that only fails at run time."""
    result = _run_validation(desired_state["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, (
        "the role's validation tasks failed against real desired state:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_role_accepts_only_immutable_managed_certificate_destinations(
    desired_state, tmp_path
):
    fingerprint = "ab" * 32
    managed = dict(desired_state["blitzecdn_nginx_sites"][0]) | {
        "certificate_mode": "uploaded",
        "certificate_path": (
            f"/etc/blitzecdn/tls/cdn-example-com/fullchain-{fingerprint}.pem"
        ),
        "certificate_key_path": (
            f"/etc/blitzecdn/tls/cdn-example-com/privkey-{fingerprint}.pem"
        ),
        "certificate_source_path": "/controller/fullchain.pem",
        "certificate_key_source_path": "/controller/privkey.pem",
    }

    accepted = _run_validation([managed], tmp_path)
    assert accepted.returncode == 0, accepted.stdout

    managed["certificate_path"] = "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem"
    rejected = _run_validation([managed], tmp_path)
    assert rejected.returncode != 0
    assert "Invalid CDN site" in rejected.stdout


def test_role_accepts_the_connecting_ip_header_without_geoip(desired_state, tmp_path):
    """It reads the connection, not a database, so no capability gates it.

    Kept in core although its GeoIP sibling moved: this asserts that
    `blitzecdn_nginx` renders the header *without* consulting anything an
    optional distribution provides, which is a statement about core's role and
    would be untestable from a package that may not be installed.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {
        "visitor_headers": {"connecting_ip": True, "ip_country": False}
    }

    result = _run_validation(sites, tmp_path)

    assert result.returncode == 0, result.stdout


def test_role_rejects_a_firewall_rule_it_cannot_safely_render(desired_state, tmp_path):
    """Defence in depth: the role holds even if the control plane regresses."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"firewall": {"denied_paths": ["/a; return 200"]}}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "firewall rules this role will not render" in result.stdout


def test_role_rejects_a_wildcard_on_an_ip_address(desired_state, tmp_path):
    """Both halves of this control have to refuse it, not just the controller.

    `*.192.0.2.1` satisfies the hostname shape check — every label of an IPv4
    literal is a valid DNS label — and nginx renders `server_name *.192.0.2.1`
    without complaint, then matches no request ever sent. The control plane
    refuses it now, and this is the redundancy: the value reaches a directive
    the role writes as root.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"server_names": ["*.192.0.2.1"]}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "Invalid CDN site" in result.stdout


# Three suites that used to be here are not any more, and their absence is the
# point rather than a gap:
#
#   * that the role refuses country rules and the BZ-IPCountry header without
#     GeoIP, and that a MaxMind key never reaches an Nginx configuration —
#     both now in `packages/blitzecdn-geoip/tests/`, executed against that
#     distribution's own role;
#   * that the role refuses Under Attack Mode without the challenge capability,
#     and that the signed njs module is written 0640 under `no_log` — now in
#     `packages/blitzecdn-security/tests/`.
#
# They moved with the implementation. A capability's refusal is asserted by the
# capability that makes it, so uninstalling the distribution takes the rule,
# the role and the test away together.
