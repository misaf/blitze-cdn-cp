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


def test_role_refuses_country_rules_without_geoip(desired_state, tmp_path):
    """The deploy must stop, and say which variable turns the feature on."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"firewall": {"denied_countries": ["RU"]}}

    result = _run_validation(sites, tmp_path, blitzecdn_edge_geoip_enabled=False)

    assert result.returncode != 0
    assert "blitzecdn_edge_geoip_enabled" in result.stdout

    allowed = _run_validation(sites, tmp_path, blitzecdn_edge_geoip_enabled=True)
    assert allowed.returncode == 0, allowed.stdout


def test_role_refuses_the_country_header_without_geoip(desired_state, tmp_path):
    """`ip_country` must fail the deploy, not be quietly dropped.

    An origin cannot tell a header that was never sent from a visitor whose
    country the database could not place, so omitting BZ-IPCountry would hand
    it a wrong answer rather than no answer. Country firewall rules already
    stop the deploy for the same reason; this shares their assertion.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {
        "visitor_headers": {"connecting_ip": True, "ip_country": True}
    }

    result = _run_validation(sites, tmp_path, blitzecdn_edge_geoip_enabled=False)

    assert result.returncode != 0
    assert "blitzecdn_edge_geoip_enabled" in result.stdout
    assert "BZ-IPCountry" in result.stdout

    allowed = _run_validation(sites, tmp_path, blitzecdn_edge_geoip_enabled=True)
    assert allowed.returncode == 0, allowed.stdout


def test_role_accepts_the_connecting_ip_header_without_geoip(desired_state, tmp_path):
    """It reads the connection, not the database; GeoIP is irrelevant to it."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {
        "visitor_headers": {"connecting_ip": True, "ip_country": False}
    }

    result = _run_validation(sites, tmp_path, blitzecdn_edge_geoip_enabled=False)

    assert result.returncode == 0, result.stdout


def test_role_refuses_under_attack_mode_without_capability_or_secret(
    desired_state, tmp_path
):
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"under_attack_mode": True}

    unsupported = _run_validation(
        sites, tmp_path, blitzecdn_nginx_under_attack_enabled=False
    )
    assert unsupported.returncode != 0
    assert "blitzecdn_nginx_under_attack_enabled is false" in unsupported.stdout

    missing_secret = _run_validation(
        sites,
        tmp_path,
        blitzecdn_nginx_under_attack_enabled=True,
        blitzecdn_nginx_under_attack_secret="",
    )
    assert missing_secret.returncode != 0
    assert "BLITZE_UNDER_ATTACK_SECRET" in missing_secret.stdout

    supported = _run_validation(
        sites,
        tmp_path,
        blitzecdn_nginx_under_attack_enabled=True,
        blitzecdn_nginx_under_attack_secret="s" * 32,
    )
    assert supported.returncode == 0, supported.stdout


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


def test_maxmind_credentials_never_reach_an_nginx_config():
    """The license key belongs in one 0600 file and nowhere else.

    /etc/nginx/conf.d is world-readable, and the geoip2 directive needs only
    the database path. A key that leaked into a template here would be
    disclosed to every account on the edge.
    """
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["dirname"] = os.path.dirname
    # The credentials are blitzecdn_edge_stack's — the role that runs the
    # updater — and are handed in here only to prove they cannot reach a
    # template that never had any business with them.
    context = _role_defaults(blitzecdn_edge_geoip_enabled=True) | {
        "blitzecdn_edge_stack_geoip_account_id": "123456",
        "blitzecdn_edge_stack_geoip_license_key": "SENTINELKEY",
    }
    nginx_side = environment.get_template("geoip.conf.j2").render(**context)
    assert "SENTINELKEY" not in nginx_side
    assert "123456" not in nginx_side
    assert context["blitzecdn_edge_runtime"]["geoip"]["database"] in nginx_side

    # …and it does reach the file that is supposed to carry it. The updater is
    # a container now, so the credential travels as a 0600 env_file rather than
    # a geoipupdate(8) configuration — and never through the Compose file, where
    # `docker inspect` would hand it to anyone who can talk to the engine.
    stack = jinja2.Environment(
        loader=jinja2.FileSystemLoader(STACK_ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    credentials = stack.get_template("geoipupdate.env.j2").render(
        blitzecdn_edge_stack_geoip_account_id="123456",
        blitzecdn_edge_stack_geoip_license_key="SENTINELKEY",
    )
    assert "GEOIPUPDATE_LICENSE_KEY=SENTINELKEY" in credentials

    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "GEOIPUPDATE_LICENSE_KEY" not in compose
    assert "blitzecdn_edge_stack_geoip_license_key" not in compose


def test_the_credentials_file_is_written_private_and_unlogged():
    """Mode and no_log are the whole protection; assert them, not the intent."""
    tasks = yaml.safe_load(
        (STACK_ROLE_DIR / "tasks/geoip-credentials.yml").read_text(encoding="utf-8")
    )

    def walk(items):
        for task in items:
            yield task
            for key in ("block", "rescue", "always"):
                if key in task:
                    yield from walk(task[key])

    writer = next(
        task
        for task in walk(tasks)
        if task.get("ansible.builtin.template", {}).get("src") == "geoipupdate.env.j2"
    )
    assert writer["ansible.builtin.template"]["mode"] == "0600"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["no_log"] is True


def test_under_attack_secret_file_is_private_and_unlogged():
    tasks = yaml.safe_load((ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8"))
    writer = next(
        task
        for task in tasks
        if task.get("ansible.builtin.template", {}).get("src") == "under-attack.js.j2"
    )

    assert writer["ansible.builtin.template"]["mode"] == "0640"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["ansible.builtin.template"]["group"] == "www-data"
    assert writer["no_log"] is True

    option = _role_spec()["blitzecdn_nginx_under_attack_secret"]
    assert option["no_log"] is True
    assert _role_defaults()["blitzecdn_nginx_under_attack_enabled"] is False
    assert _role_defaults()["blitzecdn_nginx_under_attack_passage_seconds"] == 1800


def test_the_stats_role_publishes_through_the_agreed_report_fact():
    """The channel a role returns a payload on is a contract like any other.

    `blitzecdn_stats` is the only role that returns data rather than an
    outcome, and the control plane finds it by looking for one fact name. If
    the role renamed it, every collection would come back empty with every edge
    reporting success — the silent-no-op failure this suite exists to catch.
    """
    tasks = (STATS_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    adapter = (PROJECT_DIR / "src/blitzecdn/infrastructure/ansible.py").read_text(
        encoding="utf-8"
    )

    assert 'get("blitzecdn_report")' in adapter, (
        "the Runner event adapter no longer collects blitzecdn_report; the stats "
        "role publishes it and nothing else would carry the counters back"
    )
    assert "blitzecdn_report:" in tasks, (
        "blitzecdn_stats must publish its document as the blitzecdn_report "
        "fact consumed by the Runner event adapter"
    )


def test_the_stats_role_no_longer_wants_a_controller_directory():
    """Its report travels with the run, so there is no path to hand it.

    A resurrected `blitzecdn_stats_output_dir` would be a required option the
    control plane never sets, and role argument validation would fail every
    collection.
    """
    spec = yaml.safe_load(
        (STATS_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]

    assert "blitzecdn_stats_output_dir" not in spec
