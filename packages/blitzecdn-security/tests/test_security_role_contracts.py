"""The agreements this capability's edge role has with the core it converges on.

`blitzecdn_security` puts the njs module that serves the challenge on an edge
and contributes the Nginx resource that imports it. Nothing before Nginx's
configuration test catches a disagreement: an
import that names a file the role did not write fails `nginx -t`, which is a
rolled-back deploy on every edge rather than a message naming the two
distributions that stopped agreeing.

The role is read through :data:`blitzecdn_security.ansible.ROLES_PATH` — the
same path a deployment resolves it by, rather than a directory in the checkout
that an installed wheel would not have.
"""

# ruff: noqa: F403,F405
from blitzecdn_security import ansible
from contract_support import *

ROLE = ansible.ROLES_PATH / ansible.EDGE_ROLE
TASKS = ROLE / "tasks/main.yml"
NGINX = ansible.ROLES_PATH.parents[1] / "nginx"


def _defaults(**overrides: Any) -> dict[str, Any]:
    """This role's defaults, resolved on top of the runtime contract."""
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
    assert ansible.ROLES_PATH.is_dir()
    assert [path.name for path in ansible.ROLES_PATH.iterdir()] == [ansible.EDGE_ROLE]
    assert TASKS.is_file()
    assert (ROLE / "templates/under-attack.js.j2").is_file()


def test_the_module_lands_where_the_container_mounts_it_read_only():
    """The njs directory is a contract path, mounted ro into both containers.

    A module written anywhere else would be present on the host and absent
    inside Nginx, and the `js_import` in the snippet below would fail the
    configuration test rather than the deploy saying what is missing.
    """
    resolved = _defaults(blitzecdn_security_under_attack_enabled=True)
    modules = resolved["blitzecdn_edge_runtime"]["paths"]["modules"]
    assert resolved["blitzecdn_security_under_attack_js_file"].startswith(f"{modules}/")
    mounts = "\n".join(_role_defaults()["blitzecdn_nginx_config_test_volumes"])
    assert f"{modules}:{modules}:ro" in mounts

    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "{{ blitzecdn_edge_runtime.paths.modules }}:" in compose


def test_the_snippet_imports_the_module_the_role_writes():
    """One path, spelled the same in the snippet and in the task that writes it."""
    resolved = _defaults(blitzecdn_security_under_attack_enabled=True)
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader(NGINX),
        keep_trailing_newline=True,
    )
    snippet = environment.get_template("security-http.conf.j2").render(**resolved)
    assert (
        f"js_import blitzecdn_under_attack from "
        f"{resolved['blitzecdn_security_under_attack_js_file']};" in snippet
    )


def test_package_resources_dispatch_into_the_module_this_role_installs():
    """Package Nginx resources name the njs functions its role provides.

    The names cross a distribution boundary in both directions, so both sides
    are asserted here — this is the only place that reads them together.
    """
    site = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(NGINX.glob("*.j2"))
    )
    module = (ROLE / "templates/under-attack.js.j2").read_text(encoding="utf-8")
    for entry in ("challenge", "verify", "guard"):
        assert f"js_content blitzecdn_under_attack.{entry};" in site
        assert entry in module
    assert "js_var $blitzecdn_under_attack_location;" in (
        NGINX / "security-http.conf.j2"
    ).read_text(encoding="utf-8")


def test_the_secret_file_is_private_and_unlogged():
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
        if task.get("ansible.builtin.template", {}).get("src") == "under-attack.js.j2"
    )
    assert writer["ansible.builtin.template"]["mode"] == "0640"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["ansible.builtin.template"]["group"] == "www-data"
    assert writer["no_log"] is True

    spec = yaml.safe_load(
        (ROLE / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_security_under_attack_secret"]["no_log"] is True


def test_the_capability_is_off_until_an_operator_turns_it_on():
    resolved = _defaults()
    assert resolved["blitzecdn_security_under_attack_enabled"] is False
    assert resolved["blitzecdn_security_under_attack_passage_seconds"] == 1800


def _sites_with(**overrides: Any) -> list[dict[str, Any]]:
    return [{"name": "one"} | overrides]


def test_the_role_refuses_under_attack_mode_without_capability_or_secret(tmp_path):
    sites = _sites_with(under_attack_mode=True)

    unsupported = run_role_tasks(
        TASKS, _defaults(blitzecdn_nginx_sites=sites), tmp_path
    )
    assert unsupported.returncode != 0
    assert "blitzecdn_security_under_attack_enabled" in unsupported.stdout

    missing_secret = run_role_tasks(
        TASKS,
        _defaults(
            blitzecdn_nginx_sites=sites,
            blitzecdn_security_under_attack_enabled=True,
            blitzecdn_security_under_attack_secret="",
        ),
        tmp_path,
    )
    assert missing_secret.returncode != 0
    assert "BLITZE_UNDER_ATTACK_SECRET" in missing_secret.stdout


def test_the_role_accepts_a_fleet_that_never_asks_for_the_challenge(tmp_path):
    """Installing the wheel must not change a fleet that does not use it."""
    accepted = run_role_tasks(
        TASKS,
        _defaults(blitzecdn_nginx_sites=_sites_with(under_attack_mode=False)),
        tmp_path,
    )
    assert accepted.returncode == 0, accepted.stdout


def test_the_role_rejects_a_firewall_rule_it_cannot_safely_render(
    desired_state, tmp_path
):
    """Defence in depth stays with the request-security implementation."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"firewall": {"denied_paths": ["/a; return 200"]}}

    rejected = run_role_tasks(
        TASKS,
        _defaults(blitzecdn_nginx_sites=sites),
        tmp_path,
    )

    assert rejected.returncode != 0
    assert "invalid CDN request-security rules" in rejected.stdout
