"""Helpers shared by the role-contract modules.

The Compose template these roles render, and the readers that parse a role's
defaults and build the context a converge would supply. Helpers used by one
module stay beside its tests.
"""

# ruff: noqa: F403,F405

from contract_support import *


def _defaults_of(role_dir: Path) -> dict[str, Any]:
    """A role's `defaults/main.yml`, parsed.

    Local to this module because `from contract_support import *` does not
    carry names with a leading underscore, and the helper is one line.
    """
    return yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8"))


COMPOSE_TEMPLATE = (STACK_ROLE_DIR / "templates/compose.yml.j2").read_text(
    encoding="utf-8"
)


def _edge_context(**overrides: Any) -> dict[str, Any]:
    """The variables the edge stack renders from, with references resolved.

    The paths, the image and the status endpoint are blitzecdn_edge_runtime's;
    what remains in blitzecdn_edge_stack's defaults derives from them. Resolving
    both here is what lets these tests read the *paths*, which is what the
    mounts actually are.
    """
    inputs, plain = _split_runtime(overrides)
    context: dict[str, Any] = (
        _role_defaults(**inputs) | _defaults_of(STACK_ROLE_DIR) | plain
    )
    environment = _ansible_jinja()
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


def _render_compose(**overrides: Any) -> dict[str, Any]:
    """The edge Compose project as Docker would read it, not as text."""
    context = _edge_context(**overrides)
    environment = _ansible_jinja(
        loader=jinja2.FileSystemLoader(STACK_ROLE_DIR / "templates"),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("compose.yml.j2").render(
        blitzecdn_edge_stack_resolved_image="example/edge@sha256:" + "ab" * 32,
        **context,
    )
    return yaml.safe_load(rendered)
