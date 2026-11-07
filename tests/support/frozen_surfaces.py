"""The contracts an outside party binds to, rendered as text so they can be pinned.

The HTTP routes, command names, Ansible variables and database columns are the
promises other people's files depend on. Each generator renders one surface as
sorted lines, and `tests/contract/test_frozen.py` compares them against files
under `tests/contract/frozen/`, so a rename reaching a public name is a named
failure.

All four surfaces share one line format:

    <import root>\\t<kind>\\t<detail>

The first field is the owner; every shipped interface belongs to `blitzecdn`.
Ownership is derived from callbacks, route endpoints and resource paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import click
import yaml
from sqlalchemy import CheckConstraint

from support.paths import SOURCE

#: The import package that owns every shipped interface.
ROOT = "blitzecdn"

#: Where the committed surfaces live.
FROZEN = Path(__file__).resolve().parent.parent / "contract" / "frozen"

#: The four surfaces, in the order `just refreeze` writes them.
SURFACES = ("cli", "http", "ansible", "schema")


def _owner(module: str | None) -> str:
    """The distribution a symbol came from, from its module path."""
    return (module or "").split(".")[0] or "-"


def _root_command_owner(path: str, command: Any) -> str:
    """A command no feature group claims, which may only be core's own.

    `config`, `init` and `setup` are declared on the root Typer application,
    because they run before a control plane exists. Anything else unclaimed is
    refused.
    """
    module = getattr(command.callback, "__module__", "") or ""
    if not module.startswith("blitzecdn.entrypoints.cli."):
        raise AssertionError(
            f"no CliCommandGroup claims {path!r}, and it is not core's own: "
            f"its callback is in {module!r}"
        )
    return ROOT


def _line(owner: str, kind: str, detail: str) -> str:
    return f"{owner}\t{kind}\t{detail}"


def _render(lines: list[str]) -> str:
    """Sorted, de-duplicated, newline-terminated.

    Sorted because registration order is a roster detail, not part of the
    contract.
    """
    return "\n".join(sorted(set(lines))) + "\n"


# --- the command line -------------------------------------------------------


def _parameter(param: click.Parameter) -> str:
    """One parameter, with everything a caller could break by changing it.

    Type and default are included: narrowing a choice set or flipping a default is
    a breaking change a name-only pin would miss.
    """
    if param.param_type_name == "argument":
        name = f"<{param.name}>"
    else:
        name = "/".join(param.opts + param.secondary_opts)
    kind = getattr(param.type, "name", type(param.type).__name__)
    choices = getattr(param.type, "choices", None)
    if choices:
        kind = f"choice[{','.join(str(choice) for choice in choices)}]"
    parts = [name, f"type={kind}"]
    if param.required:
        parts.append("required")
    if param.default is not None and param.param_type_name != "argument":
        parts.append(f"default={param.default!r}")
    return " ".join(parts)


def _walk_commands(
    command: click.Command, path: tuple[str, ...] = ()
) -> list[tuple[str, click.Command]]:
    children = getattr(command, "commands", None)
    if children:
        found: list[tuple[str, click.Command]] = []
        for name in sorted(children):
            found.extend(_walk_commands(children[name], (*path, name)))
        return found
    return [(" ".join(path), command)]


def cli_surface() -> str:
    """Every command an operator can type, with its options, types and defaults.

    Read from the assembled Typer application, so contributed commands appear as
    an operator finds them. Building the tree touches no database, which keeps
    this cheap.
    """
    from typer.main import get_command

    from blitzecdn.entrypoints.cli import common, main

    owners = _command_owners(common.installed_features())
    lines = []
    # Typer vendors click, so `get_command` returns `typer._click.core.Command`;
    # `_walk_commands` duck-types on `commands` either way.
    for path, command in _walk_commands(cast(click.Command, get_command(main.app))):
        owner = owners.get(path.split(" ")[0]) or _root_command_owner(path, command)
        params = " ".join(_parameter(param) for param in command.params)
        lines.append(_line(owner, "command", f"blitzecdn {path}\t{params}".rstrip()))
    return _render(lines)


def _command_owners(registry: Any) -> dict[str, str]:
    """Which feature ships each top-level command, as declared.

    Read from `CliCommandGroup.feature`, not from a callback's `__module__`, which
    names where a function was written. A named group owns its subtree; an unnamed
    one contributes root verbs. Commands on the root application itself are
    handled by `_root_command_owner`.
    """
    # Every feature ships in this distribution by construction.
    roots = {feature.name: ROOT for feature in registry.features}
    owners: dict[str, str] = {}
    for group in registry.cli_commands():
        owner = roots[group.feature]
        if group.name is not None:
            owners[group.name] = owner
            continue
        for command in group.app.registered_commands:
            name = command.name or (command.callback.__name__).replace("_", "-")
            owners[name] = owner
    return owners


# --- the HTTP API -----------------------------------------------------------


def http_surface(settings: Any) -> str:
    """Every route and every field of every schema the API publishes."""
    from blitzecdn.entrypoints.api import create_app

    app = create_app(settings)
    lines = []
    route_owners: dict[tuple[str, str], str] = {}
    for route in _api_routes(app):
        owner = _owner(route.endpoint.__module__)
        for method in sorted(route.methods):
            lines.append(_line(owner, "route", f"{method} {route.path}"))
            route_owners[(route.path, method.lower())] = owner

    document = app.openapi()
    schemas = document.get("components", {}).get("schemas", {})
    owners = _schema_owners(document, schemas, route_owners)
    for name, schema in sorted(schemas.items()):
        owner = owners.get(name, ROOT)
        required = set(schema.get("required", ()))
        for field, spec in sorted(schema.get("properties", {}).items()):
            shape = _schema_shape(spec)
            flag = "required" if field in required else "optional"
            lines.append(_line(owner, "schema", f"{name}.{field}\t{shape} {flag}"))
    return _render(lines)


def _schema_owners(
    document: dict[str, Any],
    schemas: dict[str, Any],
    route_owners: dict[tuple[str, str], str],
) -> dict[str, str]:
    """Which owner a published schema arrives with, by following `$ref`.

    A response model is only the root of what an operation publishes: nested
    models and the bodies FastAPI synthesises for uploads have no class here. So
    each operation's owner comes from its route's endpoint, and every schema
    reachable from it by `$ref` belongs to that owner. A schema two owners reach is
    shared, and shared means core.
    """
    reached: dict[str, set[str]] = {}
    for path, operations in document.get("paths", {}).items():
        for method, operation in operations.items():
            owner = route_owners.get((path, method.lower()))
            if owner is None:
                continue
            for name in _referenced(operation, schemas):
                reached.setdefault(name, set()).add(owner)
    return {
        name: owners.pop() if len(owners) == 1 else ROOT
        for name, owners in reached.items()
    }


def _referenced(node: Any, schemas: dict[str, Any]) -> set[str]:
    """Every component schema reachable from a node, transitively."""
    found: set[str] = set()
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            reference = current.get("$ref")
            if isinstance(reference, str) and reference.startswith(
                "#/components/schemas/"
            ):
                name = reference.rsplit("/", 1)[-1]
                if name not in found:
                    found.add(name)
                    pending.append(schemas.get(name))
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def _api_routes(app: Any) -> list[Any]:
    """Every routed endpoint, however deeply the application nests its routers.

    `app.routes` holds `_IncludedRouter` wrappers whose endpoints hang off
    `original_router`, so a flat pass finds only the framework's `/docs`. Walked
    duck-typed, since these classes are private to the framework; the test asserts
    a plausible count in case a change makes the walk find nothing.
    """
    found: list[Any] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        for route in getattr(node, "routes", None) or ():
            # `response_model` separates a declared route from the framework's `/docs`
            # and `/openapi.json`.
            if getattr(route, "methods", None) and hasattr(route, "response_model"):
                found.append(route)
            walk(route)
        for attribute in ("original_router", "router", "app"):
            walk(getattr(node, attribute, None))

    walk(app)
    return found


def _body_models(route: Any) -> tuple[Any, ...]:
    field = getattr(route, "body_field", None)
    annotation = getattr(field, "type_", None) if field is not None else None
    return (annotation,) if annotation is not None else ()


def _schema_shape(spec: dict[str, Any]) -> str:
    """A field's type, flattened enough to diff and to read."""
    if "$ref" in spec:
        reference = spec["$ref"]
        assert isinstance(reference, str), "$ref is not a string"
        return reference.rsplit("/", 1)[-1]
    if "enum" in spec:
        return f"enum[{','.join(str(value) for value in spec['enum'])}]"
    if "anyOf" in spec:
        return "|".join(_schema_shape(arm) for arm in spec["anyOf"])
    if spec.get("type") == "array":
        return f"array[{_schema_shape(spec.get('items', {}))}]"
    return str(spec.get("type", "any"))


# --- the Ansible interface --------------------------------------------------


def ansible_surface() -> str:
    """Every role, and every variable an operator's inventory can set.

    These names appear in operators' own inventories and desired-state documents,
    so renaming one breaks files nobody here can migrate. Read from
    `meta/argument_specs.yml`, where a role declares what it takes.
    """
    lines = []
    for root, owner in _role_roots():
        for spec in sorted(root.glob("*/meta/argument_specs.yml")):
            role = spec.parent.parent.name
            lines.append(_line(owner, "role", role))
            document = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
            for entry in (document.get("argument_specs") or {}).values():
                for option, declared in sorted((entry.get("options") or {}).items()):
                    lines.extend(
                        _line(owner, "variable", f"{role}.{path}\t{shape}")
                        for path, shape in _options(option, declared)
                    )
    return _render(lines)


def _options(path: str, declared: Any) -> list[tuple[str, str]]:
    """One line per declared key, however deeply a variable nests.

    A dotted path per leaf, so `blitzecdn_nginx_sites.ssl_mode` carries its own
    type and choices and diffs on its own line.
    """
    found = [(path, _shape(declared))]
    if isinstance(declared, dict):
        for name, nested in sorted((declared.get("options") or {}).items()):
            found.extend(_options(f"{path}.{name}", nested))
    return found


def _shape(declared: Any) -> str:
    if not isinstance(declared, dict):
        return "type=any"
    parts = [f"type={declared.get('type', 'any')}"]
    if "elements" in declared:
        parts.append(f"elements={declared['elements']}")
    if declared.get("required"):
        parts.append("required")
    if "choices" in declared:
        parts.append(f"choices=[{','.join(str(c) for c in declared['choices'])}]")
    if "default" in declared:
        parts.append(f"default={declared['default']!r}")
    return " ".join(parts)


def _role_roots() -> list[tuple[Path, str]]:
    """Every `roles/` directory in the workspace, with the wheel that ships it."""
    roots = [(roles, ROOT) for roles in sorted(SOURCE.rglob("ansible/roles"))]
    return roots


# --- the database -----------------------------------------------------------


def schema_surface() -> str:
    """Every table, column, constraint, and the revision that produces them.

    Freezing the schema beside its revision makes the two move together: after a
    release a column changes only by a new revision. Constraints are pinned too
    (`CHECK`s, `ON DELETE`), since they say what a value may be and narrowing one
    must be reconciled against existing rows.
    """
    import blitzecdn.composition  # noqa: F401  — imports every store's tables
    from blitzecdn.core.persistence.tables import Base

    lines = [_line(ROOT, "revision", _alembic_head())]
    for name, table in sorted(Base.metadata.tables.items()):
        for column in table.columns:
            parts = [
                f"type={_column_type(column.type)}",
                "null" if column.nullable else "not-null",
            ]
            if column.primary_key:
                parts.append("pk")
            if column.unique:
                parts.append("unique")
            parts.extend(
                _foreign_key(key) for key in sorted(column.foreign_keys, key=str)
            )
            lines.append(
                _line(ROOT, "column", f"{name}.{column.name}\t{' '.join(parts)}")
            )
        lines.extend(
            _line(ROOT, "check", f"{name}.{check.name}\t{check.sqltext}")
            for check in sorted(
                (c for c in table.constraints if isinstance(c, CheckConstraint)),
                key=lambda c: str(c.name or ""),
            )
        )
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            columns = ",".join(column.name for column in index.columns)
            unique = " unique" if index.unique else ""
            lines.append(
                _line(ROOT, "index", f"{name}.{index.name}\t({columns}){unique}")
            )
    return _render(lines)


def _column_type(declared: Any) -> str:
    """The declared type, not the storage it happens to compile down to.

    `UtcDateTime` renders as `VARCHAR`, but the decorator is the contract: it
    refuses naive datetimes and keeps the offset the inventory plugin parses. So
    our decorators are named beside their storage; built-in types use their
    SQL representation.
    """
    compiled = str(declared)
    own = type(declared)
    if own.__module__.startswith("blitzecdn"):
        return f"{own.__name__}({compiled})"
    return compiled


def _foreign_key(key: Any) -> str:
    """The reference and what it does when the referent goes away.

    `CASCADE` on `dns_records.domain` deletes records with the zone; `RESTRICT` on
    `dns_records.site` refuses the delete. The target name alone cannot tell them
    apart.
    """
    reference = f"fk={key.target_fullname}"
    for action in ("ondelete", "onupdate"):
        rule = getattr(key, action, None)
        if rule:
            reference += f" {action}={rule}"
    return reference


def _alembic_head() -> str:
    """The head of the revision graph, which is not the last filename.

    A branched or broken chain has no head, which Alembic reports and a `glob`
    cannot.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(SOURCE / "assets/migrations"))
    return ScriptDirectory.from_config(config).get_current_head() or "-"


__all__ = [
    "FROZEN",
    "ROOT",
    "SURFACES",
    "ansible_surface",
    "cli_surface",
    "http_surface",
    "schema_surface",
]
