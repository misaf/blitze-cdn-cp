"""The agreements this capability's edge role has with the core it converges on.

`blitzecdn_security` installs the njs challenge module and contributes the
Nginx resource that imports it. A disagreement surfaces only as a failed
`nginx -t` on every edge.

The role is read through
:data:`blitzecdn.capabilities.security.ansible.ROLES_PATH`, the path a
deployment resolves it by.
"""

from typing import Any

import yaml
from support.contracts import (
    STACK_ROLE_DIR,
    ansible_jinja,
    jinja2,
    resolve,
    role_defaults,
    run_role_tasks,
    runtime_defaults,
)

from blitzecdn.capabilities.security import ansible

ROLE = ansible.ROLES_PATH / ansible.EDGE_ROLE
TASKS = ROLE / "tasks/main.yml"
NGINX = ansible.ROLES_PATH.parents[1] / "nginx"


def _defaults(**overrides: Any) -> dict[str, Any]:
    """This role's defaults, resolved on top of the runtime contract."""
    context = runtime_defaults() | yaml.safe_load(
        (ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    )
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
    assert ansible.ROLES_PATH.is_dir()
    assert [path.name for path in ansible.ROLES_PATH.iterdir()] == [ansible.EDGE_ROLE]
    assert TASKS.is_file()
    assert (ROLE / "templates/under-attack-key.conf.j2").is_file()
    # The implementation is a configuration resource, not something the role
    # writes: it ships in the release beside the fragments that import it.
    assert (NGINX / "under-attack.js.j2").is_file()
    assert not (ROLE / "templates/under-attack.js.j2").exists()


def test_the_module_lands_where_the_container_mounts_it_read_only():
    """The njs directory is a contract path, mounted ro into both containers.

    A module written elsewhere would be absent inside Nginx.
    """
    resolved = _defaults(blitzecdn_security_under_attack_enabled=True)
    modules = resolved["blitzecdn_edge_runtime"]["paths"]["modules"]
    assert resolved["blitzecdn_security_under_attack_js_file"].startswith(f"{modules}/")
    mounts = "\n".join(role_defaults()["blitzecdn_nginx_config_test_volumes"])
    assert f"{modules}:{modules}:ro" in mounts

    compose = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(encoding="utf-8")
    assert "{{ blitzecdn_edge_runtime.paths.modules }}:" in compose


def test_the_snippet_imports_the_module_and_includes_the_key():
    """One path each, spelled the same in the snippet and where it is written."""
    resolved = _defaults(blitzecdn_security_under_attack_enabled=True)
    environment = ansible_jinja(
        loader=jinja2.FileSystemLoader(NGINX),
        keep_trailing_newline=True,
    )
    snippet = environment.get_template("security-http.conf.j2").render(**resolved)
    assert (
        f"js_import blitzecdn_under_attack from "
        f"{resolved['blitzecdn_security_under_attack_js_file']};" in snippet
    )
    assert f"include {resolved['blitzecdn_security_under_attack_key_file']};" in (
        snippet
    )


def test_nginx_resources_dispatch_into_the_module_this_role_installs():
    """The Nginx resources name the njs functions the role provides; this is the only
    place both sides are read together.
    """
    site = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(NGINX.glob("*.j2"))
    )
    module = (NGINX / "under-attack.js.j2").read_text(encoding="utf-8")
    for entry in ("challenge", "verify", "guard"):
        assert f"js_content blitzecdn_under_attack.{entry};" in site
        assert entry in module
    assert "js_var $blitzecdn_under_attack_location;" in (
        NGINX / "security-http.conf.j2"
    ).read_text(encoding="utf-8")


def test_the_secret_file_is_private_and_unlogged():
    """Mode and no_log are the whole protection; assert them, not the intent.

    The secret lives only in the root-only, edge-local key include, outside every
    configuration release.
    """

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
        if task.get("ansible.builtin.template", {}).get("src")
        == "under-attack-key.conf.j2"
    )
    assert writer["ansible.builtin.template"]["mode"] == "0600"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["ansible.builtin.template"]["group"] == "root"
    assert writer["no_log"] is True
    resolved = _defaults(blitzecdn_security_under_attack_enabled=True)
    secrets = resolved["blitzecdn_edge_runtime"]["paths"]["secrets"]
    assert resolved["blitzecdn_security_under_attack_key_file"].startswith(
        f"{secrets}/"
    )
    module = (NGINX / "under-attack.js.j2").read_text(encoding="utf-8")
    assert "blitzecdn_security_under_attack_secret" not in module

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
