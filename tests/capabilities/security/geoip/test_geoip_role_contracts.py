"""The agreements this capability's edge role has with the core it converges on.

`blitzecdn_geoip` writes the database under the runtime contract's capability
data directory and contributes the Nginx resource defining
`$blitzecdn_country`; both are agreements with core.

The role is read through
:data:`blitzecdn.capabilities.security.geoip.ansible.ROLES_PATH`, the path a
deployment resolves it by.
"""

from typing import Any

import yaml
from support.contracts import (
    STACK_ROLE_DIR,
    _ansible_jinja,
    _resolve,
    _role_defaults,
    _runtime_defaults,
    jinja2,
    run_role_tasks,
)

from blitzecdn.capabilities.security.geoip import ansible

ROLE = ansible.ROLES_PATH / ansible.EDGE_ROLE
TASKS = ROLE / "tasks/main.yml"
NGINX = ansible.ROLES_PATH.parents[1] / "nginx"


def _defaults(**overrides: Any) -> dict[str, Any]:
    """This role's defaults, resolved on top of the runtime contract.

    Every path is an expression over `blitzecdn_edge_runtime`.
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
    """The contribution is only true if the directory really carries the role."""
    assert ansible.ROLES_PATH.is_dir()
    assert [path.name for path in ansible.ROLES_PATH.iterdir()] == [ansible.EDGE_ROLE]
    assert TASKS.is_file()
    assert (NGINX / "geoip-http.conf.j2").is_file()


def test_the_database_lives_under_the_contract_data_directory():
    """Core mounts one directory read-only; anything outside it is invisible.

    Both containers mount `paths.data`; a database elsewhere would leave the
    geoip2 directive pointing at nothing.
    """
    resolved = _defaults()
    data = resolved["blitzecdn_edge_runtime"]["paths"]["data"]
    assert resolved["blitzecdn_geoip_directory"].startswith(f"{data}/")
    assert resolved["blitzecdn_geoip_database"].startswith(
        resolved["blitzecdn_geoip_directory"]
    )


def test_the_contract_data_directory_is_mounted_read_only_by_both_containers():
    """Core's half of the same agreement, asserted from this side too.

    Without the mount every country lookup stops and the edge will not start.
    """
    data = _runtime_defaults()["blitzecdn_edge_runtime"]["paths"]["data"]
    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "- {{ blitzecdn_edge_runtime.paths.data }}:" in compose
    assert f"{data}:{data}:ro" in _render_data_mounts()


def _render_data_mounts() -> str:
    """Core's configuration-test mounts, resolved to real paths."""
    return "\n".join(_role_defaults()["blitzecdn_nginx_config_test_volumes"])


def test_the_snippet_defines_the_variable_the_site_template_reads():
    """One variable name, spelled the same on both sides.

    A rename surfaces only as a failed `nginx -t`.
    """
    snippet = (NGINX / "geoip-http.conf.j2").read_text(encoding="utf-8")
    site = (NGINX / "geoip-upstream.conf.j2").read_text(encoding="utf-8")
    assert "$blitzecdn_country source=$remote_addr country iso_code;" in snippet
    assert "set $blitzecdn_visitor_ip_country $blitzecdn_country;" in site


def test_maxmind_credentials_never_reach_an_nginx_config():
    """The license key belongs in one 0600 file and nowhere else.

    /etc/nginx/conf.d is world-readable, and the geoip2 directive needs only the
    database path.
    """
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader([NGINX, ROLE / "templates"]),
        keep_trailing_newline=True,
    )
    context = _defaults(
        blitzecdn_geoip_enabled=True,
        blitzecdn_geoip_account_id="123456",
        blitzecdn_geoip_license_key="SENTINELKEY",
    )
    snippet = environment.get_template("geoip-http.conf.j2").render(**context)
    assert "SENTINELKEY" not in snippet
    assert "123456" not in snippet
    assert context["blitzecdn_geoip_database"] in snippet

    # The updater is a container, so the credential travels as a 0600 env_file,
    # never through the Compose file, where `docker inspect` would expose it.
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
    """The capability is available but not armed by default.

    On by default, every fleet would need a MaxMind credential.
    """
    assert _defaults()["blitzecdn_geoip_enabled"] is False


def _sites_with(**site_overrides: Any) -> list[dict[str, Any]]:
    return [{"name": "one", "under_attack_mode": False} | site_overrides]


def test_the_role_refuses_country_rules_when_the_capability_is_off(tmp_path):
    """The deploy must stop, and say which setting turns the capability on."""
    sites = _sites_with(firewall={"denied_countries": ["RU"]})

    refused = run_role_tasks(TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path)
    assert refused.returncode != 0
    assert "blitzecdn_geoip_enabled" in refused.stdout


def test_the_role_refuses_the_country_header_when_the_capability_is_off(tmp_path):
    """`ip_country` must fail the deploy, not be quietly dropped.

    An origin cannot tell an unsent header from a visitor the database could not
    place.
    """
    sites = _sites_with(visitor_headers={"connecting_ip": True, "ip_country": True})

    refused = run_role_tasks(TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path)
    assert refused.returncode != 0
    assert "BZ-IPCountry" in refused.stdout


def test_the_role_accepts_a_site_that_asks_for_no_country_setting(tmp_path):
    """With the capability off, a site that needs no country converges normally."""
    sites = _sites_with(visitor_headers={"connecting_ip": True, "ip_country": False})

    accepted = run_role_tasks(TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path)
    assert accepted.returncode == 0, accepted.stdout
