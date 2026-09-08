"""The shared edge runtime contract.

``blitzecdn_nginx``, ``blitzecdn_edge_stack`` and ``blitzecdn_firewall``
converge the same machine and need the same answers about it: where its files
live, which ports it listens on, where its health can be read. One contract,
read by all three, with no sibling reads left to grow back.
"""

# ruff: noqa: F403,F405

from contract_support import *

#: The roles that converge an edge and share its runtime.
EDGE_ROLES = ("blitzecdn_nginx", "blitzecdn_edge_stack", "blitzecdn_firewall")


#: The one reference across those roles that is not a contract member.
#:
#: blitzecdn_edge_stack overrides blitzecdn_nginx_config_test_image when it asks
#: that role to validate the running configuration against a *new* image. That
#: is a parameter passed to a task file, which is what an entry point is for —
#: not a read of another role's state — and blitzecdn_nginx declares it.
#:
#: blitzecdn_nginx_listeners_claimed goes the other way: the Nginx role decides
#: whether any server block claims the public ports and publishes the answer,
#: and health.yml reads that rather than re-deriving it from the site list. A
#: published output is a contract of its own; the defaults file is not.
SIBLING_EXCEPTIONS = {
    "blitzecdn_nginx_config_test_image",
    "blitzecdn_nginx_listeners_claimed",
}


def test_no_edge_role_reads_another_edge_roles_variables():
    """The coupling this contract replaced must not grow back.

    A sibling read is invisible in review and expensive in production: it makes
    the role that runs the container depend on the role that writes
    configuration, for reasons that have nothing to do with configuration, and
    it means a value can be changed in one place and silently disagree in
    another. Everything genuinely shared is blitzecdn_edge_runtime's.
    """
    for role in EDGE_ROLES:
        others = [f"{other}_" for other in EDGE_ROLES if other != role]
        for source in sorted((ROLES_DIR / role).rglob("*")):
            if source.suffix not in {".yml", ".j2"} or not source.is_file():
                continue
            document = source.read_text(encoding="utf-8")
            for line in document.splitlines():
                # Prose is where these roles explain themselves to each other,
                # and naming a sibling there is the point.
                if line.lstrip().startswith("#"):
                    continue
                for prefix in others:
                    for word in re.findall(rf"{re.escape(prefix)}[a-z0-9_]+", line):
                        assert word in SIBLING_EXCEPTIONS, (
                            f"{source.relative_to(PROJECT_DIR)} reads {word}, "
                            "which belongs to another edge role. Shared runtime "
                            "values are blitzecdn_edge_runtime's."
                        )


def test_every_edge_role_declares_the_contract_it_reads():
    """An undeclared contract is an undefined-variable error mid-converge.

    Declaring it makes a play that forgot blitzecdn_edge fail in argument
    validation, before the role has changed anything.
    """
    for role in EDGE_ROLES:
        spec = yaml.safe_load(
            (ROLES_DIR / role / "meta/argument_specs.yml").read_text(encoding="utf-8")
        )["argument_specs"]["main"]["options"]
        assert spec["blitzecdn_edge_runtime"]["required"] is True, role
        assert spec["blitzecdn_edge_runtime"]["type"] == "dict", role


def _contract_readers() -> dict[str, Path]:
    """Every role that legitimately reads the shared runtime contract.

    Core's three edge roles, and the capability roles that ship in wheels. The
    second group belongs here for the same reason the first does: a capability
    converging an edge reads `blitzecdn_edge_runtime` exactly as core's roles
    do — `blitzecdn_cache_config` owns the cache directory core also creates —
    so a member it shares with one core role is shared runtime, not a variable
    that escaped its owner. Core's tests do not otherwise reach into packages,
    and this one does only to count readers.
    """
    readers = {role: ROLES_DIR / role for role in EDGE_ROLES}
    for package in sorted((PROJECT_DIR / "packages").iterdir()):
        for directory in sorted(package.glob("src/*/ansible/roles/*")):
            if directory.is_dir():
                readers[f"{package.name}:{directory.name}"] = directory
    return readers


def test_the_contract_holds_no_value_only_one_role_uses():
    """Every member has to be read by at least two roles that converge an edge.

    Otherwise the contract becomes the place variables go to escape their
    owner, and "shared runtime" stops meaning anything. Nginx policy — cache
    sizing, ciphers, compression — stays in blitzecdn_nginx; the health timeout
    and the rollback record stay in blitzecdn_edge_stack.

    "Two roles" counts a capability's role as readily as one of core's. The
    runtime identity is the case that made the distinction matter: core's
    blitzecdn_edge_stack and the cache capability's role both create the cache
    directory, both have to own it as the uid the image's workers run as, and
    a value those two share is the definition of shared runtime even though
    only one of them is core's.
    """
    runtime = _runtime_defaults()["blitzecdn_edge_runtime"]
    sources = {
        role: "\n".join(
            source.read_text(encoding="utf-8")
            for source in sorted(directory.rglob("*"))
            if source.suffix in {".yml", ".j2"} and source.is_file()
        )
        for role, directory in _contract_readers().items()
    }

    def members(prefix: str, value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from members(f"{prefix}.{key}", item)
        else:
            yield prefix

    for member in members("blitzecdn_edge_runtime", runtime):
        readers = [role for role, text in sources.items() if member in text]
        assert len(readers) >= 2, (
            f"{member} is read only by {readers or 'nothing'}. A value one role "
            "owns belongs in that role's defaults, not in the shared contract."
        )


def test_the_shared_runtime_is_defined_in_exactly_one_place():
    """One authoritative value, which is the whole point of the exercise.

    The literals below used to appear in two and three role defaults at once,
    held together by tests asserting the copies agreed. Agreement is not
    something a single definition can fail at.
    """
    duplicated = (
        "/var/cache/nginx/blitzecdn",
        "/var/lib/blitzecdn/acme",
        "/var/lib/blitzecdn/empty",
        "/stub_status",
        "8090",
        "2052, 2082, 2086, 2095",
        "2053, 2083, 2087, 2096",
    )
    for role in EDGE_ROLES:
        defaults = (ROLES_DIR / role / "defaults/main.yml").read_text(encoding="utf-8")
        body = "\n".join(
            line for line in defaults.splitlines() if not line.lstrip().startswith("#")
        )
        for literal in duplicated:
            assert literal not in body, (
                f"{role} restates {literal!r}, which blitzecdn_edge owns. Two "
                "copies of a runtime value agree until the day one is changed."
            )
