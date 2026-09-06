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
import re
import typing
from pathlib import Path
from typing import Any

import click
import yaml
from paths import CORE_ANSIBLE, PACKAGES, SOURCE, optional_packages
from sqlalchemy import CheckConstraint

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


def _root_command_owner(path: str, command: Any) -> str:
    """A command no plugin group claims, which may only be core's own.

    `config`, `init` and `setup` are declared on the root Typer application
    directly: they configure and bootstrap the control plane, so they have to
    work before one exists to register plugins against. They ship in
    `blitzecdn.cli` and belong to core by construction.

    Anything else unclaimed is refused rather than attributed to core by
    default. A wheel's command landing here would be one a core-only run could
    not filter out of the golden, which is the exact failure the ownership
    column exists to prevent — and silence is how it would happen.
    """
    module = getattr(command.callback, "__module__", "") or ""
    if not module.startswith("blitzecdn.cli."):
        raise AssertionError(
            f"no CliCommandGroup claims {path!r}, and it is not core's own: "
            f"its callback is in {module!r}"
        )
    return ROOT


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

    from blitzecdn.cli import common, main

    owners = _command_owners(common.installed_plugins())
    lines = []
    for path, command in _walk_commands(get_command(main.app)):
        owner = owners.get(path.split(" ")[0]) or _root_command_owner(path, command)
        params = " ".join(_parameter(param) for param in command.params)
        lines.append(_line(owner, "command", f"blitzecdn {path}\t{params}".rstrip()))
    return _render(lines)


def _command_owners(registry: Any) -> dict[str, str]:
    """Which distribution ships each top-level command, as declared.

    This used to read `command.callback.__module__`, which answers where a
    function was written rather than which wheel ships it: a group whose
    commands delegate to a shared helper, or whose callbacks are wrapped by a
    decorator defined elsewhere, is attributed to whoever defined the wrapper.
    `CliCommandGroup.plugin` says it outright, and a group belongs to exactly
    one plugin, so every command beneath it does too.

    The lookup is keyed on the first word because that is the whole of what a
    group contributes: a named group owns its subtree, and an unnamed one hands
    over root verbs. Not every command comes through a group — `config`, `init`
    and `setup` are declared on the root application itself, because they run
    before there is a control plane for a plugin to be registered against —
    and `_root_command_owner` is where those are accounted for.
    """
    manager = registry._manager
    roots = {
        manager.get_name(plugin): _owner(
            getattr(plugin, "__name__", type(plugin).__module__)
        )
        for plugin in manager.get_plugins()
    }
    owners: dict[str, str] = {}
    for group in registry.cli_commands():
        owner = roots[group.plugin]
        if group.name is not None:
            owners[group.name] = owner
            continue
        for command in group.app.registered_commands:
            name = command.name or (command.callback.__name__).replace("_", "-")
            owners[name] = owner
    return owners


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

    lines = [
        _line(ROOT, "hook_api_version", str(types.HOOK_API_VERSION)),
        # The set, not just the current version: this is what actually decides
        # whether an installed wheel loads, and widening or narrowing it is the
        # compatibility decision. Pinning only `HOOK_API_VERSION` would let a
        # supported contract be dropped without the surface moving.
        _line(
            ROOT,
            "supported_hook_api_versions",
            ",".join(
                str(version) for version in sorted(types.SUPPORTED_HOOK_API_VERSIONS)
            ),
        ),
        _line(ROOT, "entry_point_group", types.ENTRY_POINT_GROUP),
        *(
            _line(ROOT, "framework", requirement)
            for requirement in _abi_frameworks(hooks, types)
        ),
    ]

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


def _abi_frameworks(*modules: Any) -> list[str]:
    """The third-party libraries this ABI is expressed in, and their bounds.

    A hookspec returns `Sequence[APIRouter]` and a contribution carries a
    `Typer`, so a wheel implementing either is written against FastAPI and
    Typer as surely as it is written against these dataclasses — and pluggy is
    the mechanism itself, since `hookimpl` is a marker every wheel applies.

    Core pins all three, so a wheel inherits the bound transitively through
    `blitzecdn>=4.0.0,<5` and pip resolves one of each. What was missing is
    that nothing recorded *which* major the contract was written in. Widening
    `typer<1` to `typer<2` would leave every line of this file identical while
    `CliCommandGroup.app` came to mean a different class, which is precisely
    the change a wheel author needs to see.

    Derived by reading what the two ABI modules import, `TYPE_CHECKING` blocks
    included, so a fourth library entering a hookspec signature brings its
    bound in here without anyone remembering to add it.
    """
    import ast
    import importlib.metadata
    import sys

    imported: set[str] = set()
    for module in modules:
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    external = imported - sys.stdlib_module_names - {ROOT, "__future__"}

    requirements = importlib.metadata.requires(ROOT) or []
    found = []
    for requirement in requirements:
        name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0].strip()
        if name in external:
            found.append(requirement.replace(" ", ""))
    missing = external - {
        re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0].strip()
        for requirement in requirements
    }
    assert not missing, (
        f"the plugin ABI is expressed in {sorted(missing)}, which core does "
        "not declare as a dependency; a wheel would inherit no bound at all"
    )
    return sorted(found)


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
    """Every published name, at the one path this project promises for it.

    `_PUBLIC_SDK_PREFIXES` in `test_packages` decides which *modules* a wheel
    may import. It says nothing about what is in them, so a symbol an installed
    package uses can be renamed or removed with the allowlist untouched. This
    is the symbol-level half.

    One path per symbol, and the shallowest one. A prefix publishes every
    module beneath it, so a package that re-exports through its `__init__`
    published each of its names twice — `CliCommandGroup` arrived as both
    `core.plugins.CliCommandGroup` and `core.plugins.types.CliCommandGroup`,
    and `resolve_role_search_path` three times, once for every level of
    `resolution`. Sixty-three of a hundred and seventy-five lines were the same
    promise written again at a deeper address.

    Pinning the deep one is worse than redundant: it freezes the layout behind
    the façade. Splitting `types.py`, or moving a class between the modules
    under `resolution/`, would fail this file having changed nothing a wheel
    can see — while the re-export it actually imports stayed exactly where it
    was. So a name reachable from a published ancestor under the same identity
    is recorded at the ancestor, and the module it happens to live in becomes
    an implementation detail again.

    A name the façade does *not* re-export keeps its own path, because that
    path is the only way to reach it — `core.ports.operations` is published
    module by module for that reason, and so is `hookspec`, which core uses to
    declare hookspecs and no `__init__` hands on.
    """
    import importlib
    import pkgutil

    modules: dict[str, Any] = {}
    for prefix in public_prefixes:
        for module_name in _modules_under(prefix, importlib, pkgutil):
            try:
                modules[module_name] = importlib.import_module(module_name)
            except Exception:  # a module that needs a live control plane
                continue

    lines = []
    for module_name, module in modules.items():
        for name in _exported(module, module_name):
            if _shallower_path(module_name, name, modules) is not None:
                continue
            shape = _symbol(module, name)
            lines.append(_line(ROOT, "sdk", f"{module_name}.{name}\t{shape}"))
    return _render(lines)


def _shallower_path(module_name: str, name: str, modules: dict[str, Any]) -> str | None:
    """A published ancestor package that re-exports this very object, if any.

    Identity, not the name. `AuditEvent` is published twice on purpose —
    `api.models.AuditEvent` is the wire shape and `core.domain.audit.AuditEvent`
    is the domain model, two different classes that share a leaf name — and
    collapsing those would erase a real promise rather than a duplicated one.
    Only an ancestor holding the *same object* is the same promise.
    """
    parts = module_name.split(".")
    here = getattr(modules[module_name], name, _ABSENT)
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = ".".join(parts[:depth])
        if ancestor in modules and getattr(modules[ancestor], name, _ABSENT) is here:
            return ancestor
    return None


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
    """Every table, column, constraint, and the revision that produces them.

    One migration exists today and it is still editable, because nothing is
    installed. That stops being true at the first release: from then on a column
    is changed by adding a revision, never by editing this one. Freezing the
    schema beside its revision is what makes the two move together — a column
    changed without a new revision fails here rather than on somebody's upgrade.

    Names and nullability are the cheap half of a schema, and for a while they
    were the only half this file pinned. The half that carries meaning is the
    part that says what a value may *be*: `type IN ('A', 'AAAA')`, `ttl BETWEEN
    1 AND 604800`, and the `RESTRICT` that refuses to delete a site hostnames
    still route to. Those are also the expensive half — widening a `CHECK` is
    free, narrowing one has to be reconciled against rows that already exist —
    so they are the ones most worth holding still, and every one of them used
    to move without moving a line here.
    """
    from sqlmodel import SQLModel

    import blitzecdn.composition  # noqa: F401  — imports every store's tables

    lines = [_line(ROOT, "revision", _alembic_head())]
    for name, table in sorted(SQLModel.metadata.tables.items()):
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
                key=lambda c: c.name or "",
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

    `UtcDateTime` is a `TypeDecorator` over `String`, so it renders as
    `VARCHAR` — indistinguishable from a plain string column. The decorator is
    the whole contract: it refuses to store a naive datetime and it keeps the
    UTC offset in the text, which is what lets the Ansible inventory plugin
    parse these values with the standard library. Swapping it for `String`
    would break both and leave this file unmoved, so name the class and keep
    the storage beside it.

    Only *our* decorators are named. SQLModel wraps every implicit `str` field
    in an `AutoString`, which is storage plumbing and not a promise this
    project makes: naming it would mean an explicit `Column(String)` and a bare
    `name: str` render differently, so a pure refactor would move the golden
    and the next person to read a diff here would learn to skim it.
    """
    compiled = str(declared)
    own = type(declared)
    if own.__module__.startswith("blitzecdn"):
        return f"{own.__name__}({compiled})"
    return compiled


def _foreign_key(key: Any) -> str:
    """The reference and what it does when the referent goes away.

    `ondelete` is a behavioural difference of the first order: `CASCADE` on
    `dns_records.domain` deletes the records with the zone, and `RESTRICT` on
    `dns_records.site` refuses the delete instead. Turning one into the other
    silently discards an operator's records, and the target name alone cannot
    tell them apart.
    """
    reference = f"fk={key.target_fullname}"
    for action in ("ondelete", "onupdate"):
        rule = getattr(key, action, None)
        if rule:
            reference += f" {action}={rule}"
    return reference


def _alembic_head() -> str:
    """The head of the revision graph, which is not the last filename.

    Sorting `versions/*.py` answers a question about the directory listing, and
    what this file needs to pin is a question about the graph: which revision
    an upgrade actually stops at. The two agree while there is one revision and
    diverge the moment a second is named something that does not sort last —
    and a branched or broken chain has no head at all, which Alembic reports
    and a `glob` cannot.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(SOURCE / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head() or "-"


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
