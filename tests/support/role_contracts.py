"""Helpers shared by the role-contract modules.

The Compose template these roles render, and the readers that parse a role's
defaults and build the context a converge would supply. Helpers used by one
module stay beside its tests.
"""

from pathlib import Path
from typing import Any, cast

import yaml

from support.contracts import (
    STACK_ROLE_DIR,
    ansible_jinja,
    jinja2,
    role_defaults,
    role_path,
    split_runtime,
)

RELEASE_ROLE_DIR = role_path("blitzecdn_config_release")


def defaults_of(role_dir: Path) -> dict[str, Any]:
    """A role's `defaults/main.yml`, parsed, without resolving the runtime contract."""
    return cast(
        dict[str, Any],
        yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8")),
    )


COMPOSE_TEMPLATE = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(
    encoding="utf-8"
)


def edge_context(**overrides: Any) -> dict[str, Any]:
    """The variables the edge stack renders from, with references resolved.

    The stack's defaults derive from blitzecdn_edge_runtime, so both are resolved
    to read the paths the mounts actually use. The release root comes from
    blitzecdn_config_release, which the edge play always runs first.
    """
    inputs, plain = split_runtime(overrides)
    context: dict[str, Any] = (
        role_defaults(**inputs)
        | defaults_of(RELEASE_ROLE_DIR)
        | defaults_of(STACK_ROLE_DIR)
        | plain
    )
    environment = ansible_jinja()
    for _ in range(len(context)):
        unresolved = {
            name: value
            for name, value in context.items()
            if isinstance(value, str) and "{{" in value
        }
        if not unresolved:
            break
        for name, value in unresolved.items():
            context[name] = environment.from_string(value).render(**context).strip()
        if unresolved.keys() == {
            name
            for name, value in context.items()
            if isinstance(value, str) and "{{" in value
        }:
            break
    return context


def render_compose(**overrides: Any) -> dict[str, Any]:
    """The edge Compose project as Docker would read it, not as text."""
    context = edge_context(**overrides)
    environment = ansible_jinja(
        loader=jinja2.FileSystemLoader(STACK_ROLE_DIR / "templates"),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("compose.yml.j2").render(
        blitzecdn_edge_stack_resolved_image="example/edge@sha256:" + "ab" * 32,
        **context,
    )
    return cast(dict[str, Any], yaml.safe_load(rendered))


def probe_volumes(**overrides: Any) -> list[str]:
    """The configuration test's mounts, as the edge play binds them.

    blitzecdn_config_release's select.yml sets these before blitzecdn_nginx runs;
    blitzecdn_nginx has no default of its own.
    """
    select = yaml.safe_load(
        (RELEASE_ROLE_DIR / "tasks/select.yml").read_text(encoding="utf-8")
    )
    (volumes,) = (
        task["ansible.builtin.set_fact"]["blitzecdn_nginx_config_test_volumes"]
        for task in select
        if "blitzecdn_nginx_config_test_volumes"
        in task.get("ansible.builtin.set_fact", {})
    )
    context = edge_context(**overrides)
    environment = ansible_jinja()
    return [environment.from_string(volume).render(**context) for volume in volumes]
