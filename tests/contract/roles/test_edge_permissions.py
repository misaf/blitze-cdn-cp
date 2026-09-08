"""What the converged edge may read and write, and who owns it."""

# ruff: noqa: F403,F405

from contract_support import *


def _cache_directory_declarations(path: Path) -> list[dict]:
    """Every mapping in a task file that gives the cache directory an owner.

    Found by walking the loaded YAML rather than by reading lines: one writer
    declares the path and its owner as an inline `loop` entry and the other as
    module arguments, and a line-oriented check passes on whichever it was
    written against.
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

    Nginx chowns its cache root to the user its workers run as when it starts,
    and that user belongs to the runtime image, not to the host: Alpine's
    `nginx` is 101 where Ubuntu's `www-data` is 33, and only the uid crosses a
    bind mount. A host-side name here is a chown on every converge — the edge
    setting it to the one, the next deploy setting it back to the other, and
    no fleet ever reporting itself converged.

    Both writers are checked together because they run in one converge:
    `blitzecdn_cache_config` in the capability slot, `blitzecdn_edge_stack` in
    its pre-task. Agreeing with Nginx in one and not the other just moves the
    loop to the other task.
    """
    writers = {
        "blitzecdn_edge_stack": STACK_ROLE_DIR / "tasks/prepare.yml",
        "blitzecdn_cache_config": (
            PROJECT_DIR
            / "packages/blitzecdn-cache/src/blitzecdn_cache/ansible/roles"
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

    Walked rather than grepped for the same reason the cache one is: the two
    writers spell it as a `loop` entry and as module arguments, and the token
    is a `copy` whose `dest` is the path rather than a `path`.
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("path", "dest"):
                declared = node.get(key)
                # `state: absent` is the task that withdraws a token once the
                # authority has read it. It names the path and grants no
                # access, which is the whole of what it should do.
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

    Nginx serves `/.well-known/acme-challenge/` from a read-only bind mount of
    this tree, and its workers are the image's uid — 101 — not a host account.
    Owning the directory `root:www-data` at 0750 gave the worker neither the
    owner bits nor the group, so it fell to `other`, which 0750 leaves empty:
    every HTTP-01 validation on the fleet answered 403 for a file that was
    sitting right there, and ACME reports that as a failed challenge without
    ever saying why.

    Both writers are checked together because they create the same directory
    in one lifecycle — `blitzecdn_nginx` on every converge, the challenge
    playbook each time a certificate is issued — and disagreeing about its
    mode would make them take turns, which is the cache bug again.
    """
    writers = {
        "blitzecdn_nginx": ROLE_DIR / "tasks/main.yml",
        "acme-challenge": (
            PROJECT_DIR
            / "packages/blitzecdn-certificates/src/blitzecdn_certificates"
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

    Top level because that is what `-e name=value` can reach. A sub-option —
    a per-site `connecting_ip`, an `ssl_mode` inside the site document — comes
    from the control plane already typed and cannot be handed to a play as a
    loose string, so a bare conditional on one is not this bug.
    """
    specs = list(ROLES_DIR.glob("*/meta/argument_specs.yml"))
    specs += list(
        (PROJECT_DIR / "packages").glob(
            "*/src/*/ansible/roles/*/meta/argument_specs.yml"
        )
    )
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

    An operator who writes `-e blitzecdn_edge_teardown_remove_data=false` is
    saying keep the TLS material, the configuration tree, the ACME state and
    the cache. A bare `when:` on that variable read "false" as true and
    destroyed all four — the argument spec's `type: bool` does not reach a
    value that arrives on the command line. ansible-core 2.19 and later refuse
    a non-boolean conditional rather than guessing, which turns the silent
    version of this into a failed play, but only on a version that new and
    only when the path is actually run. Twelve conditionals were written this
    way, in core and in three capability wheels.

    So: a conditional on a variable some argument spec calls a bool has to end
    in a filter that makes it one.
    """
    booleans = _declared_boolean_variables()
    assert booleans, "no argument spec declares a boolean; this test found nothing"

    sources = list(ROLES_DIR.glob("*/tasks/*.yml"))
    sources += list(ROLES_DIR.glob("*/templates/*.j2"))
    packages = PROJECT_DIR / "packages"
    sources += list(packages.glob("*/src/*/ansible/roles/*/tasks/*.yml"))
    sources += list(packages.glob("*/src/*/ansible/roles/*/templates/*.j2"))

    # Three shapes, because the first version of this test knew only the first
    # and the teardown went on failing on the other two in the same file: a
    # conditional standing alone, one behind `not`, and a Jinja `if` — in a
    # template, or mid-expression where a list is being built.
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
