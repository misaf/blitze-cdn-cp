"""Package layout boundaries."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from package_boundary_support import (
    _built_in_capabilities,
    _import_package,
    _packages,
    _source_files,
)
from paths import CORE_DOCKER, REPO_ROOT, SOURCE, optional_packages

# --- the layout inside a package is a vocabulary, not a habit ---------------


#: The modules a package's Python is organised into. Held to a closed set for
#: the same reason `ALLOWED_CAPABILITY_DEPENDENCIES` is one: ten packages that
#: converged on a shape by imitation give the eleventh author ten examples and
#: no rule, and the shape is what makes a capability readable without reading
#: it — `plugin.py` is what it contributes, `composition.py` is how it is
#: built, `ports.py` is what it calls. A package uses as few of these as it
#: needs; `blitzecdn-compression` is a `plugin.py` and nothing else.
#:
#: Growing the set is allowed and is a decision: add the name here with the
#: sentence saying what belongs in it, and to the canonical package in
#: PLUGINS.md. What this refuses is the name that arrives without either.
_PACKAGE_MODULES = {
    "__init__.py",
    "plugin.py",  # metadata and the hooks it contributes through
    "composition.py",  # builds its service from what the platform publishes
    "config.py",  # its own settings, read from its CapabilityConfig
    "ports.py",  # the narrow Protocols this capability calls
    "cli.py",  # its command groups
    "policy.py",  # its configuration contract: the values a site carries
}


#: The layers a capability may spell as a directory when it outgrows one file,
#: whose *contents* are then named after what they are rather than after the
#: layer: `archive.py`, `playbooks.py`, `checks.py`, `reporting.py`,
#: `snapshots.py`, `readiness.py`. Flat — a layer package with a package inside
#: it is a split this size of code does not have.
#:
#: `adapters.py`, `preflight.py` and `reporting.py` were single names in the
#: module set beside `service.py`, which meant the same job was documented
#: twice depending on whether a capability had grown past one file, and a
#: second adapter arriving as `renderer.py` was undocumented. The directory
#: says which layer it is, and `test_layering` reads the same directories to
#: decide which rule a file lives under.
#:
#: `api` is one of them rather than the fixed `{models.py, routes.py}` it was.
#: That set said "a package whose HTTP surface needs a third module is
#: describing something that is not an HTTP adapter", and
#: `diagnostics/api/readiness.py` is the counter-example: `/health` and
#: `/ready` are a second router because they carry a different auth posture
#: from the operator routes beside them, which is as HTTP-adapter as it gets.
#: A layer that has outgrown one file names its parts after what they are —
#: the rule every other layer here already follows.
#: `cli` joined them when `sites/cli.py` reached 572 lines — the largest module
#: in any capability, and the one place a slice's layers were still a single
#: file no matter how many capabilities' switches it edited. Its contents are
#: named after the contract whose fields they change (`tls.py`, `http.py`,
#: `security.py`), the same rule `adapters/` and `domain/` already follow, so a
#: capability that grows a switch has one place to add its command.
_FREE_FORM_DIRECTORIES = {"adapters", "api", "cli", "domain", "policy", "service"}


#: The four a capability spells as a directory whichever size it is, which is
#: why `domain.py` and `service.py` left the module set above.
#:
#: They were "a file until it outgrows one", and what that produced was a tree
#: where the same layer was a file in five capabilities and a directory in two,
#: so a reader learned the shape from whichever slice they happened to open and
#: a contributor had to decide, per slice, which spelling this one used. The
#: four here are the layers the architecture rules classify and a slice's
#: substance lives in; `policy` and `cli` stay either way, because a contract is
#: often one class and a command group is often one screen of commands.
#:
#: The cost is real and was accepted: six built-in layers and nine in the wheels
#: became directories holding one module, and `__init__` re-exports what the
#: file used to. What it buys is that `dns.service` and `deployments.service`
#: are the same kind of thing to import, to read, and to add a second module to.
_MANDATORY_LAYER_DIRECTORIES = {"adapters", "api", "domain", "service"}


#: What a package ships for something other than Python to read. `ansible/`
#: carries one module — the `importlib.resources` anchor — and its roles and
#: plays; `nginx/` carries templates only. A `.py` file deeper in either is a
#: capability putting logic where no test looks for it.
_ANSIBLE_DIRECTORIES = {"roles", "playbooks"}


_NGINX_SUFFIX = ".conf.j2"


#: Modules outside the vocabulary, by distribution, each with the reason.
#: `acme_hook` is a process entry point rather than part of the capability's
#: composition: certbot execs it as a one-shot subprocess with no control
#: plane in the picture, which is why `test_layering` also names it beside
#: `blitzecdn.composition` as a second composition root.
_DECLARED_EXTRA_MODULES = {"blitzecdn-certificates": {"acme_hook.py"}}


def _module_offence(parts: tuple[str, ...], *, nested: bool) -> str | None:
    head, *rest = parts
    if not rest:
        if head.removesuffix(".py") in _MANDATORY_LAYER_DIRECTORIES:
            layer = head.removesuffix(".py")
            return (
                f"{head} is a layer: spell it {layer}/ and name the module "
                "inside it for what it holds"
            )
        return (
            None if head in _PACKAGE_MODULES else f"{head} is not a documented module"
        )
    if head == "ansible":
        if rest == ["__init__.py"]:
            return None
        return "ansible/ ships roles and plays; its only module is the anchor"
    if head == "nginx":
        return "nginx/ ships templates, not Python"
    if head in _FREE_FORM_DIRECTORIES:
        if len(rest) != 1:
            return f"{head}/ is flat"
        # The rule PLUGINS.md states and nothing checked: what is inside a
        # layer is named for what it *is*. `domain/domain.py` says the layer
        # twice and the content never, and it is what a mechanical conversion
        # from a file produces if nobody stops it.
        if rest[0].removesuffix(".py") == head:
            return f"{head}/{rest[0]} names the layer again rather than its contents"
        return None
    if nested:
        return f"{head}/ nests a capability two levels deep"
    return _module_offence(tuple(rest), nested=True)


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_a_package_organises_its_python_into_the_documented_modules(package: Path):
    """The canonical package in PLUGINS.md, checked against the real tree.

    A capability may nest *one* level — `blitzecdn-certificates` holds
    `certificates/` and `automatic_ssl/`, which are two jobs on one capability
    rather than two capabilities — and each nested directory is held to the
    same vocabulary. A second level is a package that wants to be two
    distributions, and it says so here rather than three refactors later.
    """
    root = package / "src" / _import_package(package)
    allowed_extra = _DECLARED_EXTRA_MODULES.get(package.name, set())
    offenders = [
        f"{path.relative_to(root)}: {offence}"
        for path in _source_files(package)
        for parts in (path.relative_to(root).parts,)
        if not (len(parts) == 1 and parts[0] in allowed_extra)
        for offence in (_module_offence(parts, nested=False),)
        if offence
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "capability", _built_in_capabilities(), ids=lambda path: path.name
)
def test_a_built_in_capability_organises_its_python_the_same_way(capability: Path):
    """One vocabulary, whichever side of the packaging boundary a slice is on.

    The rule above walked `packages/` only, so the eight capabilities in
    `src/` — the ones an author reads first, and copies — were held to nothing.
    `tests/architecture/test_layering.py` then had to identify a slice's entry
    adapters from a literal set of file names, `{"cli.py", "routes.py",
    "readiness.py"}`, because a name it did not list was a name no rule
    described. `readiness.py` was on that list because somebody remembered to
    add it, and a slice growing a `commands.py` or a `websocket.py` would have
    had no entry rule at all.

    Applying this here closes the set instead of lengthening the list: a
    capability's root modules are the documented eight, everything else is
    inside a layer directory, and `_entry_files` can say `api/` and `cli.py`
    and be exhaustive rather than hopeful.

    The built-in vocabulary is the package one exactly. `policy.py` reads as
    the exception and is not: PLUGINS.md has always listed it in the canonical
    package, and a wheel may own a contract — what a wheel *cannot* do is be
    the only place one lives, because a detached capability's settings still
    have to parse.
    """
    offenders = [
        f"{path.relative_to(capability)}: {offence}"
        for path in sorted(capability.rglob("*.py"))
        if "__pycache__" not in path.parts
        for offence in (
            _module_offence(path.relative_to(capability).parts, nested=False),
        )
        if offence
    ]
    assert offenders == []


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_a_package_ships_its_ansible_and_its_templates_where_the_layout_says(
    package: Path,
):
    """The two directories the control plane locates by path, not by import.

    Core composes `roles_path` from what a contribution answers with and hands
    `templates_path` to the renderer, so a file in the wrong place here is not
    a tidiness question: it is a role Ansible will not resolve or a fragment
    the renderer will not find, and both fail on an edge rather than in CI.
    """
    root = package / "src" / _import_package(package)
    offenders = []
    ansible = root / "ansible"
    if ansible.is_dir():
        offenders += [
            f"ansible/{entry.name} is neither the anchor, roles/ nor playbooks/"
            for entry in sorted(ansible.iterdir())
            if entry.name not in {"__init__.py", "__pycache__"}
            and not (entry.is_dir() and entry.name in _ANSIBLE_DIRECTORIES)
        ]
    nginx = root / "nginx"
    if nginx.is_dir():
        offenders += [
            f"nginx/{path.relative_to(nginx)} is not a {_NGINX_SUFFIX} template"
            for path in sorted(nginx.rglob("*"))
            if path.is_file()
            and "__pycache__" not in path.parts
            and not path.name.endswith(_NGINX_SUFFIX)
        ]
    assert offenders == []


#: The contract a capability keeps behind when its wheel is detached: the
#: `__init__` re-exporting it, the contract itself, and the registration that
#: makes core aware of it. A capability holding anything else — a service, an
#: adapter, a router, a command — is implemented here.
_CONTRACT_MODULES = {"__init__.py", "policy.py", "plugin.py"}


def _implemented_here(capability: Path) -> bool:
    return any(
        path.name not in _CONTRACT_MODULES and "policy" not in path.parts
        for path in capability.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _capability_map() -> list[tuple[tuple[str, ...], str]]:
    """The table in `capabilities/__init__.py`, as rows of (capabilities, by)."""
    rows: list[tuple[tuple[str, ...], str]] = []
    for line in (
        (SOURCE / "capabilities/__init__.py").read_text(encoding="utf-8").splitlines()
    ):
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("| ").split("|")]
        if len(cells) != 3 or cells[1] in {"implemented by", "---"}:
            continue
        rows.append((tuple(re.findall(r"`([^`]+)`", cells[0])), cells[1]))
    return rows


def test_the_capability_map_says_what_the_tree_does():
    """`capabilities/__init__.py` carries the map. This is why it can be trusted.

    Two kinds of directory sit under `capabilities/` and look identical from
    the tree: a full slice, and a contract whose implementation ships as a
    wheel. That is deliberate — a contract has to load when its wheel is
    detached, so a stored site reads back and the deployment is refused by
    *name* rather than failing to parse — and the map is the only place the
    difference is written down.

    It was written down and nothing checked it, and a hand-kept table with a
    row per capability and a column per wheel is one that drifts. Each half is
    answerable from the tree: a capability implemented here holds more than its
    contract and its registration, and a wheel named in the second column
    either exists in `packages/` or does not.

    Every distribution has to appear in the docstring somewhere, including the
    four that own no site setting and so have no row — `blitzecdn-backup`,
    `blitzecdn-hardening`, `blitzecdn-origins` and `blitzecdn-resolver` — so an
    eleventh wheel cannot arrive without the map acknowledging it.
    """
    document = (SOURCE / "capabilities/__init__.py").read_text(encoding="utf-8")
    rows = _capability_map()
    directories = {path.name for path in _built_in_capabilities()}
    distributions = {path.name for path in optional_packages()}

    assert {name for names, _ in rows for name in names} == directories
    pairs = [(name, by) for names, by in rows for name in names]
    assert sorted(pairs) == sorted(set(pairs)), "a row is repeated"

    said_itself = {name for names, by in rows if by == "itself" for name in names}
    said_wheel = {name for names, by in rows if by != "itself" for name in names}
    assert said_itself == {
        path.name for path in _built_in_capabilities() if _implemented_here(path)
    }
    assert said_wheel == directories - said_itself

    named = {
        wheel
        for _, by in rows
        for wheel in re.findall(r"`([^`]+)`", by)
        if by != "itself"
    }
    assert named <= distributions, (
        f"named but not shipped: {sorted(named - distributions)}"
    )
    assert [wheel for wheel in sorted(distributions) if wheel not in document] == [], (
        "every distribution appears in the map, row or not"
    )


def test_the_documented_layout_and_the_enforced_one_are_the_same():
    """PLUGINS.md carries the vocabulary; this file refuses departures from it.

    Two registers of one rule drift, and the one that drifts is the prose,
    because nothing fails when it does. A name enforced here and absent from
    the canonical package is a rule a capability author cannot read.
    """
    documented = (REPO_ROOT / "PLUGINS.md").read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _PACKAGE_MODULES | _FREE_FORM_DIRECTORIES
        if name != "__init__.py" and name not in documented
    )
    assert missing == []


# --- the image build inputs ship inside the package, like the Ansible --------


def test_the_published_docker_paths_are_the_ones_in_the_checkout():
    """`blitzecdn.docker` and the checkout tree are the same directory.

    The module resolves through `importlib.resources`, which in a checkout
    with an editable install answers the checkout — so every other suite can
    read the constants and still be reading the files under review. The day
    that stops being true is the day the packaging moved and nothing said so.
    """
    from blitzecdn import docker

    assert docker.EDGE_CONTEXT == CORE_DOCKER / "edge"
    assert docker.CONTROL_PLANE_DOCKERFILE == CORE_DOCKER / "control-plane/Dockerfile"
    for constant in (
        docker.EDGE_DOCKERFILE,
        docker.EDGE_MODULE_PROBE_CONF,
        docker.CONTROL_PLANE_DOCKERFILE,
        docker.CONTROL_PLANE_DOCKERIGNORE,
    ):
        assert constant.is_file(), constant


def test_no_tracked_file_names_the_old_top_level_docker_directory():
    """The build inputs moved into the wheel; nothing may point back.

    A textual guard, because that is the shape of the regression: a new script
    or workflow copying the old `docker/edge` incantation would work in a
    checkout and fail on an installed controller, which is the exact failure
    mode the move removed. Python callers should read the constants; the
    justfile, the two root-run shell scripts and the release workflow cannot
    import anything, so they spell the path under `src/blitzecdn/` and this
    only refuses the pre-move form.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git, "this asks git which files are tracked"
    tracked = subprocess.run(  # noqa: S603 - fixed argv built here
        [git, "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    offenders = [
        f"{name}:{number}"
        for name in tracked
        for path in ((REPO_ROOT / name),)
        # This module is the one file that has to spell the pre-move form:
        # the needles below are it. Prose elsewhere is deliberately *not*
        # exempt — a comment telling somebody to build `docker/edge` sends
        # them somewhere that no longer exists just as surely as a command
        # would.
        if path.is_file()
        and not name.startswith("src/blitzecdn/docker/")
        and path != Path(__file__).resolve()
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        )
        for stale in ("docker/edge", "docker/control-plane")
        if stale in line and f"src/blitzecdn/{stale}" not in line
    ]
    assert offenders == [], (
        "these name the pre-move `docker/` directory, which only a checkout "
        f"has: {offenders}"
    )
