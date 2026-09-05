"""The contracts an outside party binds to, rendered as text so they can be pinned.

Everything in `tests/architecture/` guards the shape of the source tree — which
directory a module sits in, which layer may import which. All of it is fixable
with `git mv` on any afternoon. What nobody outside this repository can see, and
what no test held, is the other set: the HTTP routes, the command names, the
hook signatures, the Ansible variables and the database columns. Those are the
promises, and after the first `pip install` they are the expensive ones.

So each generator here renders one such surface as sorted lines, and
`tests/contract/test_frozen.py` compares them against files committed under
`tests/contract/frozen/`. A rename that reaches a public name stops being a
green diff and becomes a named failure.

**The line format is one format for all six surfaces**, because the comparison,
the ownership filter and the refreeze recipe are then written once:

    <import root>\\t<kind>\\t<detail>

The first field is the distribution that *promises* the line — `blitzecdn` for
the control plane, `blitzecdn_cache` for what arrives with that wheel. It is
there because the suite runs in two configurations: `just test` with every
optional distribution installed, and `just test-core-only` with none of them.
One golden file serves both, because the test filters it to whatever is
installed rather than keeping two copies that would drift apart.

Ownership is asked of the code rather than declared: a command's callback, a
route's endpoint and a role's directory each say which package they came from.
Nothing here imports an optional distribution — `test_the_control_plane_suite_
names_no_optional_package` forbids it, and these read `__module__` strings and
file paths instead.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib.util
import inspect
import typing
from pathlib import Path
from typing import Any

import click
import yaml
from paths import CORE_ANSIBLE, PACKAGES, SOURCE, optional_packages

#: The root distribution's import package. Everything else is a wheel.
ROOT = "blitzecdn"

#: Where the committed surfaces live.
FROZEN = Path(__file__).resolve().parent / "contract" / "frozen"

#: The six surfaces, in the order `just refreeze` writes them.
SURFACES = ("cli", "http", "plugin_abi", "sdk", "ansible", "schema")


def installed(owner: str) -> bool:
    """Whether this environment actually has the distribution behind a line.

    Shared with `contract/test_frozen`, which filters the golden file by it, so
    the question "is this surface present here" has exactly one answer. Asked of
    the import system rather than of the checkout: `packages/blitzecdn-cache`
    is on disk in every clone, and whether its roles and routes are part of
    *this* control plane is decided by whether the wheel is installed.
    """
    if owner == ROOT:
        return True
    try:
        return importlib.util.find_spec(owner) is not None
    except (ImportError, ValueError):
        return False


def _owner(module: str | None) -> str:
    """The distribution a symbol came from, from its module path."""
    return (module or "").split(".")[0] or "-"


def _line(owner: str, kind: str, detail: str) -> str:
    return f"{owner}\t{kind}\t{detail}"


def _render(lines: list[str]) -> str:
    """Sorted, de-duplicated, newline-terminated.

    Sorted rather than kept in discovery order: registration order is an
    implementation detail of the plugin manager, and a golden file that changed
    when a plugin moved in `BUILTIN_PLUGINS` would fail for a reason that is not
    about the contract.
    """
    return "\n".join(sorted(set(lines))) + "\n"


# --- the command line -------------------------------------------------------


def _parameter(param: click.Parameter) -> str:
    """One parameter, with everything a caller could break by changing it.

    The type and the default are included, not just the name. Narrowing a
    choice set or flipping a default is a breaking change that a name-only pin
    would let through, and both are the kind of thing a refactor does by
    accident.
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

    Read from the assembled Typer application rather than from the source, so a
    command contributed by an installed wheel is in here exactly as an operator
    would find it. Importing `blitzecdn.cli.main` builds the tree and touches no
    database — that property is the reason `blitzecdn --help` is fast, and it is
    what makes this generator cheap enough to run in the suite.
    """
    from typer.main import get_command

    from blitzecdn.cli import main

    lines = []
    for path, command in _walk_commands(get_command(main.app)):
        owner = _owner(getattr(command.callback, "__module__", None))
        params = " ".join(_parameter(param) for param in command.params)
        lines.append(_line(owner, "command", f"blitzecdn {path}\t{params}".rstrip()))
    return _render(lines)


# --- the HTTP API -----------------------------------------------------------


def http_surface(settings: Any) -> str:
    """Every route and every field of every schema the API publishes.

    Rendered as lines rather than kept as the OpenAPI document itself. Two
    reasons, and the first is the ownership column: a JSON document cannot be
    filtered to the installed distributions, so a core-only run would need a
    second copy of it. The second is that a diff of this is readable in review,
    which is the point of freezing it at all — a reordered `required` array in
    a 3,000-line JSON blob is noise, and a removed field is not.
    """
    from blitzecdn.api import create_app

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
    """Which distribution a published schema arrives with, by following `$ref`.

    The first attempt read `route.response_model.__name__`, and a core-only run
    caught it: `CertificateRequest` and the generated
    `Body_upload_certificate_...` were attributed to `blitzecdn`, so the golden
    kept them and an environment without `blitzecdn-certificates` could not
    filter them out. A response model is only the *root* of what an operation
    publishes — the nested models it references, and the bodies FastAPI
    synthesises for form and file uploads, have no class this code ever sees.

    So ownership is taken from the document instead: each operation is reached
    from a route whose endpoint names its distribution, and every schema
    reachable from that operation by `$ref` belongs to it. A schema two
    distributions reach is shared, and shared means core — it has to exist in a
    control plane that has neither.
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

    Walked rather than read off `app.routes`, which in this FastAPI holds
    `_IncludedRouter` wrappers: each contributed router appears there as an
    opaque object whose own `routes` is `None` and whose endpoints hang off
    `original_router`. A flat pass finds the framework's `/docs` and nothing a
    capability published.

    Duck-typed through the attributes a wrapper might nest under, for the same
    reason `_walk_commands` duck-types on `commands` — these classes are private
    to the framework and have changed shape at least once already, and a pin
    that silently found *no* routes would be worse than no pin at all. The test
    asserts a plausible count for that reason.
    """
    found: list[Any] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        for route in getattr(node, "routes", None) or ():
            # `response_model` is what separates a route this project declared
            # from the framework's own `/docs` and `/openapi.json`, which are
            # plain Starlette routes and nobody here promises.
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
        return spec["$ref"].rsplit("/", 1)[-1]
    if "enum" in spec:
        return f"enum[{','.join(str(value) for value in spec['enum'])}]"
    if "anyOf" in spec:
        return "|".join(_schema_shape(arm) for arm in spec["anyOf"])
    if spec.get("type") == "array":
        return f"array[{_schema_shape(spec.get('items', {}))}]"
    return str(spec.get("type", "any"))


# --- the plugin ABI ---------------------------------------------------------


def plugin_abi_surface() -> str:
    """What a third-party capability wheel is written against.

    The hookspecs and the contribution dataclasses, with their signatures and
    field types. This is the one surface where a silent change breaks code this
    repository cannot see or test: an installed wheel implements these hooks and
    constructs these dataclasses, and a renamed field is an AttributeError in
    somebody else's package.

    `HOOK_API_VERSION` is included as a line of its own, so the version and the
    contract it names move in the same diff or not at all.
    """
    from blitzecdn.core.plugins import hooks, types

    lines = [_line(ROOT, "hook_api_version", str(types.HOOK_API_VERSION))]
    lines.append(_line(ROOT, "entry_point_group", types.ENTRY_POINT_GROUP))

    for name, function in sorted(vars(hooks).items()):
        if not name.startswith("blitzecdn_") or not inspect.isfunction(function):
            continue
        signature = inspect.signature(function)
        lines.append(_line(ROOT, "hook", f"{name}{_signature(signature)}"))

    for name, declared in sorted(vars(types).items()):
        if not inspect.isclass(declared) or declared.__module__ != types.__name__:
            continue
        if dataclasses.is_dataclass(declared):
            for field in dataclasses.fields(declared):
                default = _default(field)
                lines.append(
                    _line(
                        ROOT,
                        "contribution",
                        f"{name}.{field.name}: {_annotation(field.type)}{default}",
                    )
                )
        elif issubclass(declared, enum.Enum):
            lines.extend(
                _line(ROOT, "enum", f"{name}.{member.name} = {member.value!r}")
                for member in declared
            )
    return _render(lines)


def _signature(signature: inspect.Signature) -> str:
    """Names, annotations and defaults — a default is part of the contract.

    Rendered rather than taken from `str(signature)`, which prints the module
    path of every annotation and would churn this file on an import move that
    changed nothing a caller can see.
    """
    parameters = []
    for name, parameter in signature.parameters.items():
        rendered = f"{name}: {_annotation(parameter.annotation)}"
        if parameter.default is not inspect.Parameter.empty:
            rendered += f" = {parameter.default!r}"
        parameters.append(rendered)
    return f"({', '.join(parameters)}) -> {_annotation(signature.return_annotation)}"


def _annotation(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "-"
    if isinstance(annotation, str):
        return annotation
    return typing.get_type_hints and getattr(annotation, "__name__", str(annotation))


def _default(field: dataclasses.Field[Any]) -> str:
    if field.default is not dataclasses.MISSING:
        return f" = {field.default!r}"
    if field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return " = <factory>"
    return ""


# --- the published SDK ------------------------------------------------------


def sdk_surface(public_prefixes: tuple[str, ...]) -> str:
    """The names inside each published prefix, not just the prefix.

    `_PUBLIC_SDK_PREFIXES` in `test_packages` decides which *modules* a wheel
    may import. It says nothing about what is in them, so a symbol an installed
    package uses can be renamed or removed with the allowlist untouched. This
    is the symbol-level half: for every published module, the public names it
    exports.
    """
    import importlib
    import pkgutil

    lines = []
    for prefix in public_prefixes:
        for module_name in _modules_under(prefix, importlib, pkgutil):
            try:
                module = importlib.import_module(module_name)
            except Exception:  # a module that needs a live control plane
                continue
            for name in _exported(module, module_name):
                shape = _symbol(module, name)
                lines.append(_line(ROOT, "sdk", f"{module_name}.{name}\t{shape}"))
    return _render(lines)


def _symbol(module: Any, name: str) -> str:
    """What the name *is*, not merely that it was listed.

    A module's `__all__` is a list of strings, and a string survives the
    function it names being renamed. Recording only the names let
    `package_directory` become `resolve_package_directory` with this surface
    unchanged and the pin green — which is exactly the break it exists to
    catch, since an installed wheel calls that function by name.

    So the shape is recorded too: `missing` when `__all__` promises something
    the module does not have, and a signature for anything callable, because a
    parameter a caller passes is as much of the contract as the name is.
    """
    value = getattr(module, name, _ABSENT)
    if value is _ABSENT:
        return "missing"
    if inspect.isclass(value):
        bases = ",".join(base.__name__ for base in value.__bases__)
        return f"class({bases})"
    if inspect.isfunction(value) or inspect.ismethod(value):
        return f"def{_signature(inspect.signature(value))}"
    if callable(value):
        return f"callable {type(value).__name__}"
    return type(value).__name__


_ABSENT = object()


def _exported(module: Any, module_name: str) -> list[str]:
    """A module's own public names, not everything it happens to import.

    `__all__` decides where a module declares one — that is the module saying
    what it publishes, and several here re-export a sibling's symbol
    deliberately. Where there is none, a name counts only if it was *defined*
    in this module: without that test the surface fills up with `Annotated`,
    `Path` and every framework class the module imported, and the golden file
    then churns on refactors that changed nothing anyone can import.
    """
    exported = getattr(module, "__all__", None)
    if exported is not None:
        return sorted(exported)
    names = []
    for name, value in vars(module).items():
        if name.startswith("_") or inspect.ismodule(value):
            continue
        if getattr(value, "__module__", module_name) != module_name:
            continue
        names.append(name)
    return sorted(names)


def _modules_under(prefix: str, importlib: Any, pkgutil: Any) -> list[str]:
    try:
        module = importlib.import_module(prefix)
    except Exception:
        return []
    if not hasattr(module, "__path__"):
        return [prefix]
    return [
        prefix,
        *(info.name for info in pkgutil.walk_packages(module.__path__, f"{prefix}.")),
    ]


# --- the Ansible interface --------------------------------------------------


def ansible_surface() -> str:
    """Every role, and every variable an operator's inventory can set.

    The largest surface here — and the only one that lands in files this
    project does not own. A role name and a `blitzecdn_*` variable appear in an
    operator's own inventory and in the desired-state documents they keep, so
    renaming one is a breaking change for people whose files nobody here can
    migrate.

    Read from `meta/argument_specs.yml`, which is where a role declares what it
    takes, rather than from `defaults/main.yml`, which is where it says what it
    would do without being told.
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

    This was one line per *top-level* variable, with a nested structure
    flattened to `keys=[a,b,c]` — which pinned the names of the keys and
    nothing else about them. `blitzecdn_nginx_sites` is the whole site document,
    twenty-five keys deep, and under that rendering `ssl_mode` could have lost a
    choice or `max_upload_size` changed its default with the golden file
    unmoved. The largest surface here was the least pinned.

    A dotted path per leaf instead, so `blitzecdn_nginx_sites.ssl_mode` carries
    its own type and choices and diffs on its own line.
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
    roots = [(CORE_ANSIBLE / "roles", ROOT)]
    for package in optional_packages():
        source = package / "src"
        for import_root in sorted(source.iterdir()) if source.is_dir() else []:
            roles = import_root / "ansible" / "roles"
            # Read from the checkout, but reported only when the wheel is
            # installed: this is the one surface whose files are on disk in
            # every clone whether or not the distribution is part of this
            # control plane, and a core-only run would otherwise see roles that
            # `resolve_role_search_path` would never put on the search path.
            if roles.is_dir() and installed(import_root.name):
                roots.append((roles, import_root.name))
    return roots


# --- the database -----------------------------------------------------------


def schema_surface() -> str:
    """Every table and column, and the migration revision that produces them.

    One migration exists today and it is still editable, because nothing is
    installed. That stops being true at the first release: from then on a column
    is changed by adding a revision, never by editing this one. Freezing the
    schema beside its revision is what makes the two move together — a column
    changed without a new revision fails here rather than on somebody's upgrade.
    """
    from sqlmodel import SQLModel

    import blitzecdn.composition  # noqa: F401  — imports every store's tables

    lines = [_line(ROOT, "revision", _alembic_head())]
    for name, table in sorted(SQLModel.metadata.tables.items()):
        for column in table.columns:
            parts = [
                f"type={column.type}",
                "null" if column.nullable else "not-null",
            ]
            if column.primary_key:
                parts.append("pk")
            parts.extend(
                f"fk={key.target_fullname}"
                for key in sorted(column.foreign_keys, key=str)
            )
            lines.append(
                _line(ROOT, "column", f"{name}.{column.name}\t{' '.join(parts)}")
            )
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            columns = ",".join(column.name for column in index.columns)
            unique = " unique" if index.unique else ""
            lines.append(
                _line(ROOT, "index", f"{name}.{index.name}\t({columns}){unique}")
            )
    return _render(lines)


def _alembic_head() -> str:
    revisions = sorted((SOURCE / "migrations" / "versions").glob("*.py"))
    return revisions[-1].stem if revisions else "-"


__all__ = [
    "FROZEN",
    "PACKAGES",
    "ROOT",
    "SURFACES",
    "ansible_surface",
    "cli_surface",
    "http_surface",
    "installed",
    "plugin_abi_surface",
    "schema_surface",
    "sdk_surface",
]
