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
    ansible_jinja,
    jinja2,
    resolve,
    run_role_tasks,
    runtime_defaults,
)
from support.role_contracts import probe_volumes

from blitzecdn.capabilities.security.geoip import ansible

ROLE = ansible.ROLES_PATH / ansible.EDGE_ROLE
TASKS = ROLE / "tasks/main.yml"
NGINX = ansible.ROLES_PATH.parents[1] / "nginx"


def _defaults(**overrides: Any) -> dict[str, Any]:
    """This role's defaults, resolved on top of the runtime contract.

    Every path is an expression over `blitzecdn_edge_runtime`.
    """
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    # Where the controller keeps its databases: a controller-side `lookup` and
    # a digest only a release supplies, so Ansible resolves them lazily.
    defaults.pop("blitzecdn_geoip_controller_directory")
    context = runtime_defaults() | defaults
    context |= overrides
    environment = ansible_jinja()
    for _ in range(len(context)):
        plain = {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }
        resolved = resolve(plain, context, environment)
        if resolved == plain:
            break
        context |= resolved
    return context


def test_the_role_this_capability_ships_is_where_it_says_it_is():
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
    data = runtime_defaults()["blitzecdn_edge_runtime"]["paths"]["data"]
    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "- {{ blitzecdn_edge_runtime.paths.data }}:" in compose
    assert f"{data}:{data}:ro" in _render_data_mounts()


def _render_data_mounts() -> str:
    """Core's configuration-test mounts, resolved to real paths."""
    return "\n".join(probe_volumes())


def test_the_snippet_defines_the_variable_the_site_template_reads():
    """One variable name, spelled the same on both sides.

    A rename surfaces only as a failed `nginx -t`.
    """
    snippet = (NGINX / "geoip-http.conf.j2").read_text(encoding="utf-8")
    site = (NGINX / "geoip-upstream.conf.j2").read_text(encoding="utf-8")
    assert "$blitzecdn_country source=$remote_addr country iso_code;" in snippet
    assert "set $blitzecdn_visitor_ip_country $blitzecdn_country;" in site


def test_the_snippet_names_only_the_database_path():
    """/etc/nginx/conf.d is world-readable; the geoip2 directive needs a path."""
    environment = ansible_jinja(
        loader=jinja2.FileSystemLoader([NGINX]),
        keep_trailing_newline=True,
    )
    context = _defaults(blitzecdn_geoip_enabled=True)
    snippet = environment.get_template("geoip-http.conf.j2").render(**context)
    assert f"geoip2 {context['blitzecdn_geoip_database']} {{" in snippet


def _walk(items):
    for task in items:
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _walk(task[key])


def _tasks() -> list[dict[str, Any]]:
    return list(_walk(yaml.safe_load(TASKS.read_text(encoding="utf-8"))))


def test_an_edge_holds_no_maxmind_credential_and_no_schedule():
    """The controller downloads for the fleet; an edge only receives the file.

    A credential on every edge is exposed by compromising any one of them, and a
    download per edge spends MaxMind's per-account daily limit.
    """
    assert not (ROLE / "templates").exists()
    spec = yaml.safe_load(
        (ROLE / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    names = set(spec) | set(_defaults())
    for forbidden in ("license", "account", "updater", "units", "calendar"):
        assert not [name for name in names if forbidden in name], forbidden
    text = TASKS.read_text(encoding="utf-8")
    assert "lookup('env'" not in text
    assert "systemd" not in text
    assert "docker_compose" not in text


def test_the_pinned_database_is_verified_before_and_after_the_copy():
    """The digest the release captured is checked on the controller, then on the
    edge: a file replaced on the controller after compilation never ships.
    """
    tasks = _tasks()
    index = {id(task): position for position, task in enumerate(tasks)}
    read = next(
        task
        for task in tasks
        if task.get("ansible.builtin.stat", {}).get("path")
        == "{{ blitzecdn_geoip_database_source }}"
    )
    assert read["delegate_to"] == "localhost"
    assert read["ansible.builtin.stat"]["checksum_algorithm"] == "sha256"

    refused = next(
        task
        for task in tasks
        if "blitzecdn_geoip_database_sha256"
        in str(task.get("ansible.builtin.assert", {}).get("that", ""))
        and "blitzecdn_geoip_source" in str(task["ansible.builtin.assert"]["that"])
    )
    copy = next(task for task in tasks if "ansible.builtin.copy" in task)
    assert copy["ansible.builtin.copy"]["src"] == (
        "{{ blitzecdn_geoip_database_source }}"
    )
    assert copy["ansible.builtin.copy"]["dest"] == "{{ blitzecdn_geoip_database }}"
    assert copy["ansible.builtin.copy"]["mode"] == "0644"

    installed = next(
        task
        for task in tasks
        if task.get("name") == "Require the installed database to match the release"
    )
    assert index[id(read)] < index[id(refused)] < index[id(copy)]
    assert index[id(copy)] < index[id(installed)]


def test_the_capability_is_off_until_an_operator_turns_it_on():
    """The capability is available but not armed by default.

    On by default, every fleet would need a GeoIP database.
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


def test_the_role_finds_the_pinned_file_where_the_controller_stores_it():
    """Directory and file name agree with the store, so a digest names one file."""
    from blitzecdn.capabilities.security.geoip.domain import database_filename
    from blitzecdn.capabilities.security.geoip.wiring import DATABASE_DIRECTORY

    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    directory = defaults["blitzecdn_geoip_controller_directory"]
    assert directory == (
        "{{ lookup('env', 'BLITZE_STATE_DIR') }}/" + DATABASE_DIRECTORY
    )
    install = next(
        task
        for task in _tasks()
        if task.get("name") == "Install the database the release pins"
    )
    source = install["vars"]["blitzecdn_geoip_database_source"].replace(" ", "")
    digest = "{{blitzecdn_geoip_database_sha256}}"
    assert source == (
        "{{blitzecdn_geoip_controller_directory}}/"
        + database_filename("0" * 64).replace("0" * 64, digest)
    )


def test_the_role_runs_on_a_fleet_whose_controller_has_no_database(tmp_path):
    """No digest pinned: validating the role's arguments must not need one.

    Including the role validates its argument spec, which renders every
    default; a default naming `blitzecdn_geoip_database_sha256` failed every
    fleet whose controller had not downloaded a database yet.
    """
    tasks = tmp_path / "include.yml"
    tasks.write_text(
        yaml.safe_dump([{"ansible.builtin.include_role": {"name": str(ROLE)}}]),
        encoding="utf-8",
    )
    variables = {
        "blitzecdn_edge_runtime": runtime_defaults()["blitzecdn_edge_runtime"],
        "blitzecdn_nginx_sites": _sites_with(),
    }

    result = run_role_tasks(tasks, variables, tmp_path)
    assert result.returncode == 0, result.stdout
