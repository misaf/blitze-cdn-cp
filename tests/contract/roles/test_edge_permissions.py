"""What the converged edge may read and write, and who owns it."""

import re
from pathlib import Path

import yaml
from support.contracts import (
    PROJECT_DIR,
    ROLE_DIR,
    ROLES_DIR,
    STACK_ROLE_DIR,
    contributed_roles,
)


def _cache_directory_declarations(path: Path) -> list[dict]:
    """Every mapping in a task file that gives the cache directory an owner.

    Walked through the loaded YAML, since one writer uses an inline `loop` entry
    and the other module arguments.
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            declared = node.get("path")
            if isinstance(declared, str) and "paths.cache" in declared:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def test_every_writer_of_the_cache_directory_owns_it_as_the_runtime_worker():
    """Two roles create this directory, and Nginx has an opinion about it.

    Nginx chowns its cache root to its worker user at start, and that user is the
    image's (Alpine's `nginx` is 101, Ubuntu's `www-data` 33; only the uid crosses
    a bind mount). A host-side name would mean a chown on every converge. Both
    writers run in one converge, so both are checked.
    """
    writers = {
        "blitzecdn_edge_stack": STACK_ROLE_DIR / "tasks/prepare.yml",
        "blitzecdn_cache_config": (
            PROJECT_DIR
            / "src/blitzecdn/capabilities/cache/ansible/roles"
            / "blitzecdn_cache_config/tasks/main.yml"
        ),
    }
    for role, path in writers.items():
        declarations = _cache_directory_declarations(path)
        assert declarations, f"{role} no longer creates the cache directory"
        for declaration in declarations:
            owner = str(declaration.get("owner", ""))
            assert "worker_uid" in owner, (
                f"{role} owns the cache directory as {owner!r} rather than "
                "blitzecdn_edge_runtime.worker_uid. Nginx will chown it to "
                "the image's worker uid, and the two will take turns on "
                "every converge"
            )


def _acme_challenge_declarations(path: Path) -> list[dict]:
    """Every mapping that gives the challenge directory or a token a mode.

    Walked for the same reason as the cache one; the token is a `copy` whose
    `dest` is the path.
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("path", "dest"):
                declared = node.get(key)
                # `state: absent` withdraws a token once the authority has read it; it
                # grants no access.
                if (
                    isinstance(declared, str)
                    and "acme-challenge" in declared
                    and node.get("state") != "absent"
                ):
                    found.append(node)
                    break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def test_the_edge_can_read_the_acme_challenge_it_is_asked_to_serve():
    """The webroot is written on the host and read from inside the container.

    Nginx serves `/.well-known/acme-challenge/` from a read-only bind mount, as
    uid 101, not a host account. `root:www-data` at 0750 leaves that uid only the
    empty `other` bits, so every HTTP-01 validation answered 403. Both writers
    (`blitzecdn_nginx` and the challenge playbook) must agree on the mode.
    """
    writers = {
        "blitzecdn_nginx": ROLE_DIR / "tasks/main.yml",
        "acme-challenge": (
            PROJECT_DIR
            / "src/blitzecdn/capabilities/tls"
            / "ansible/playbooks/acme-challenge.yml"
        ),
    }
    for role, path in writers.items():
        declarations = _acme_challenge_declarations(path)
        assert declarations, f"{role} no longer writes the challenge directory"
        for declaration in declarations:
            owner = str(declaration.get("owner", ""))
            mode = str(declaration.get("mode", ""))
            # `other` is what a container's uid falls to, so either the worker
            # owns it or the last digit has to carry the access.
            reachable = "worker_uid" in owner or (mode[-1:] or "0") in "1234567"
            assert reachable, (
                f"{role} writes {declaration.get('path') or declaration.get('dest')} "
                f"as owner={owner!r} mode={mode!r}, which the edge's Nginx "
                "worker cannot read: it is the image's uid, so it matches "
                "neither a host owner nor a host group, and 0750 leaves "
                "nothing for `other`"
            )


def _declared_boolean_variables() -> set[str]:
    """Role variables an argument spec declares as a bool, top level only.

    Only top-level variables can arrive as strings through `-e name=value`.
    """
    specs = list(ROLES_DIR.glob("*/meta/argument_specs.yml"))
    for role in contributed_roles().values():
        specs += list(role.glob("meta/argument_specs.yml"))
    declared: set[str] = set()
    for spec in specs:
        document = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        for entry in (document.get("argument_specs") or {}).values():
            for name, definition in (entry.get("options") or {}).items():
                if isinstance(definition, dict) and definition.get("type") == "bool":
                    declared.add(name)
    return declared


def test_no_conditional_trusts_a_boolean_it_was_handed_as_a_string():
    """`-e name=value` is a string, and every non-empty string is true.

    `-e blitzecdn_edge_teardown_remove_data=false` under a bare `when:` would
    destroy the data it asks to keep; the argument spec's `type: bool` does not
    reach command-line values. So a conditional on a bool-declared variable must
    end in a filter that makes it one.
    """
    booleans = _declared_boolean_variables()
    assert booleans, "no argument spec declares a boolean; this test found nothing"

    sources = list(ROLES_DIR.glob("*/tasks/*.yml"))
    sources += list(ROLES_DIR.glob("*/templates/*.j2"))
    for role in contributed_roles().values():
        sources += list(role.glob("tasks/*.yml"))
        sources += list(role.glob("templates/*.j2"))

    # Three shapes: a conditional alone, one behind `not`, and a Jinja `if` in a
    # template or mid-expression.
    forms = (
        re.compile(r"\s*(?:when:|-)\s+([A-Za-z_][\w.]*)\s*$"),
        re.compile(r"\bnot\s+([A-Za-z_]\w*)\b"),
        re.compile(r"\bif\s+([A-Za-z_]\w*)\b"),
    )
    bare = []
    for path in sorted(sources):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for form in forms:
                for found in form.finditer(line):
                    name = found.group(1)
                    if name.split(".")[0] not in booleans:
                        continue
                    if re.match(r"\s*\|\s*bool", line[found.end() :]):
                        continue
                    bare.append(f"{path.relative_to(PROJECT_DIR)}:{number}: {name}")
    bare = sorted(dict.fromkeys(bare))

    assert bare == [], (
        "these conditionals test a declared boolean without coercing it, so a "
        "value passed with `-e name=value` arrives as a string and every "
        "string is true:\n  " + "\n  ".join(bare)
    )
