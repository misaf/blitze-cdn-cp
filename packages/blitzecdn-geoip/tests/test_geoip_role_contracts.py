"""The agreements this capability's edge role has with the core it converges on.

These moved here with the role. `blitzecdn_geoip` writes two things onto an
edge — a database under the runtime contract's capability data directory, and
the `conf.d` snippet defining `$blitzecdn_country` that core's site template
reads — and each one is a place where core and this package have to agree
without either importing the other.

Core's side is read through the shared contract helpers; this package's side is
read through :data:`blitzecdn_geoip.ansible.ROLES_PATH` — the same path a
deployment resolves the role by, rather than a directory in the checkout that
an installed wheel would not have.
"""

# ruff: noqa: F403,F405
from blitzecdn_geoip import ansible
from contract_support import *

ROLE = ansible.ROLES_PATH / ansible.EDGE_ROLE
TASKS = ROLE / "tasks/main.yml"


def _defaults(**overrides: Any) -> dict[str, Any]:
    """This role's defaults, resolved on top of the runtime contract.

    Resolved because Ansible resolves: every path here is written as an
    expression over `blitzecdn_edge_runtime`, and a test comparing template
    source against a literal proves nothing.
    """
    context = _runtime_defaults() | yaml.safe_load(
        (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
    context |= overrides
    environment = _ansible_jinja()
    for _ in range(len(context)):
        plain = {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }
        resolved = _resolve(plain, context, environment)
        if resolved == plain:
            break
        context |= resolved
    return context


def test_the_role_this_distribution_ships_is_where_it_says_it_is():
    """The contribution is only true if the directory really carries the role.

    Everything below reads this directory, so a wheel built without its Ansible
    tree would make the rest of this file fail with `FileNotFoundError` rather
    than with the thing it is actually about — and a deploy would fail with
    "the role was not found", naming nothing that leads back to this package.
    """
    assert ansible.ROLES_PATH.is_dir()
    assert [path.name for path in ansible.ROLES_PATH.iterdir()] == [ansible.EDGE_ROLE]
    assert TASKS.is_file()
    assert (ROLE / "templates/geoip.conf.j2").is_file()


def test_the_database_lives_under_the_contract_data_directory():
    """Core mounts one directory read-only; anything outside it is invisible.

    The edge container and the configuration-test container both mount
    `paths.data` and nothing else a capability writes. A database provisioned
    anywhere else would be present on the host, absent inside Nginx, and the
    edge would fail to start with a geoip2 directive pointing at nothing.
    """
    resolved = _defaults()
    data = resolved["blitzecdn_edge_runtime"]["paths"]["data"]
    assert resolved["blitzecdn_geoip_directory"].startswith(f"{data}/")
    assert resolved["blitzecdn_geoip_database"].startswith(
        resolved["blitzecdn_geoip_directory"]
    )


def test_the_contract_data_directory_is_mounted_read_only_by_both_containers():
    """Core's half of the same agreement, asserted from this side too.

    This package cannot edit core's Compose template or the configuration-test
    mounts, so the only guard against one of them dropping the data mount is a
    test that reads them. Without the mount every country lookup on the fleet
    stops resolving, and the failure appears as an edge that will not start.
    """
    data = _runtime_defaults()["blitzecdn_edge_runtime"]["paths"]["data"]
    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert f"- {{{{ blitzecdn_edge_runtime.paths.data }}}}:" in compose
    assert f"{data}:{data}:ro" in _render_data_mounts()


def _render_data_mounts() -> str:
    """Core's configuration-test mounts, resolved to real paths."""
    return "\n".join(_role_defaults()["blitzecdn_nginx_config_test_volumes"])


def test_the_snippet_defines_the_variable_the_site_template_reads():
    """One variable name, spelled the same in two distributions.

    Nothing at run time catches a rename: nginx refuses an undefined variable
    at config-test time, which is a rolled-back deploy rather than a message
    naming the two files that disagree.
    """
    snippet = (ROLE / "templates/geoip.conf.j2").read_text(encoding="utf-8")
    site = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    assert "$blitzecdn_country source=$remote_addr country iso_code;" in snippet
    assert "$blitzecdn_country" in site


def test_the_snippet_is_written_where_nginx_will_include_it():
    resolved = _defaults()
    nginx = resolved["blitzecdn_edge_runtime"]["paths"]["nginx"]
    assert resolved["blitzecdn_geoip_conf_file"] == f"{nginx}/conf.d/blitzecdn-geoip.conf"


def test_maxmind_credentials_never_reach_an_nginx_config():
    """The license key belongs in one 0600 file and nowhere else.

    /etc/nginx/conf.d is world-readable, and the geoip2 directive needs only
    the database path. A key that leaked into the snippet would be disclosed to
    every account on the edge.
    """
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader(ROLE / "templates"),
        keep_trailing_newline=True,
    )
    context = _defaults(
        blitzecdn_geoip_enabled=True,
        blitzecdn_geoip_account_id="123456",
        blitzecdn_geoip_license_key="SENTINELKEY",
    )
    snippet = environment.get_template("geoip.conf.j2").render(**context)
    assert "SENTINELKEY" not in snippet
    assert "123456" not in snippet
    assert context["blitzecdn_geoip_database"] in snippet

    # …and it does reach the file that is supposed to carry it. The updater is
    # a container, so the credential travels as a 0600 env_file rather than a
    # geoipupdate(8) configuration — and never through the Compose file, where
    # `docker inspect` would hand it to anyone who can talk to the engine.
    credentials = environment.get_template("geoipupdate.env.j2").render(**context)
    assert "GEOIPUPDATE_LICENSE_KEY=SENTINELKEY" in credentials

    compose = (ROLE / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "GEOIPUPDATE_LICENSE_KEY" not in compose
    assert "blitzecdn_geoip_license_key" not in compose


def test_the_credentials_file_is_written_private_and_unlogged():
    """Mode and no_log are the whole protection; assert them, not the intent."""

    def walk(items):
        for task in items:
            yield task
            for key in ("block", "rescue", "always"):
                if key in task:
                    yield from walk(task[key])

    tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    writer = next(
        task
        for task in walk(tasks)
        if task.get("ansible.builtin.template", {}).get("src") == "geoipupdate.env.j2"
    )
    assert writer["ansible.builtin.template"]["mode"] == "0600"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["no_log"] is True

    spec = yaml.safe_load(
        (ROLE / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_geoip_license_key"]["no_log"] is True


def test_the_capability_is_off_until_an_operator_turns_it_on():
    """Installing the distribution provides the capability; it does not arm it.

    A default of true would put a MaxMind credential requirement on every edge
    of every fleet that happened to install this wheel, and fail the deploy of
    a fleet that never asked for country rules.
    """
    assert _defaults()["blitzecdn_geoip_enabled"] is False


def _sites_with(**site_overrides: Any) -> list[dict[str, Any]]:
    return [{"name": "one", "under_attack_mode": False} | site_overrides]


def test_the_role_refuses_country_rules_when_the_capability_is_off(tmp_path):
    """The deploy must stop, and say which setting turns the feature on."""
    sites = _sites_with(firewall={"denied_countries": ["RU"]})

    refused = run_role_tasks(
        TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path
    )
    assert refused.returncode != 0
    assert "blitzecdn_geoip_enabled" in refused.stdout


def test_the_role_refuses_the_country_header_when_the_capability_is_off(tmp_path):
    """`ip_country` must fail the deploy, not be quietly dropped.

    An origin cannot tell a header that was never sent from a visitor whose
    country the database could not place, so omitting BZ-IPCountry would hand
    it a wrong answer rather than no answer.
    """
    sites = _sites_with(visitor_headers={"connecting_ip": True, "ip_country": True})

    refused = run_role_tasks(
        TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path
    )
    assert refused.returncode != 0
    assert "BZ-IPCountry" in refused.stdout


def test_the_role_accepts_a_site_that_asks_for_no_country_setting(tmp_path):
    """The capability being off is normal, not an error, for everyone else.

    The whole point of a detachable capability is that a fleet which does not
    use it converges exactly as before. A blanket refusal here would make
    installing the wheel a breaking change for every site on the fleet.
    """
    sites = _sites_with(visitor_headers={"connecting_ip": True, "ip_country": False})

    accepted = run_role_tasks(
        TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path
    )
    assert accepted.returncode == 0, accepted.stdout
