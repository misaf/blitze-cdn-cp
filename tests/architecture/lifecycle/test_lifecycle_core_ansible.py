"""Core's own Ansible travels the same way a capability's does."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    PLATFORM_ROLES,
    _environment,
)
from paths import REPO_ROOT

#: The plays core passes to `run_playbook` by path, plus the two it hands to
#: Ansible as configuration. The inventory plugin is in the list because it is
#: the piece with no capability precedent: Ansible loads inventory plugins by
#: *directory*, so a wheel that carried the roles and left the plugin in the
#: checkout would resolve every role and then find no fleet to run them on.
PLATFORM_PLAYBOOKS = (
    "control-plane.yml",
    "decommission.yml",
    "edge.yml",
    "uninstall.yml",
)


def test_the_root_wheel_carries_the_platform_ansible_tree(wheels: dict[str, Path]):
    """Core keeps the contract it holds every capability to.

    `test_the_capability_wheel_carries_its_whole_ansible_tree` asserts this of
    `blitzecdn-cache`, and the reason given there is that a wheel shipping only
    the `.py` files leaves a controller pointing at a directory that is not
    there. Core is the larger case of exactly that: without this, `pip install
    blitzecdn` produces a control plane that cannot converge anything, and the
    only reason it works today is that `install.sh` and the Dockerfile copy the
    repository in behind it. That makes the checkout an undeclared runtime
    dependency of the root distribution — invisible until someone installs the
    wheel the way its own packaging says they may.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn/ansible"
    for role in PLATFORM_ROLES:
        assert f"{root}/roles/{role}/tasks/main.yml" in names
        assert f"{root}/roles/{role}/meta/argument_specs.yml" in names
    for playbook in PLATFORM_PLAYBOOKS:
        assert f"{root}/playbooks/{playbook}" in names
    # The dynamic inventory plugin and the source file that selects it. The
    # fleet lives in the control-plane database and this is how Ansible reaches
    # it; a wheel without them has roles and no hosts.
    assert f"{root}/plugins/inventory/blitzecdn.py" in names
    assert f"{root}/inventory/blitzecdn.yml" in names
    # Shipped non-secret defaults. Read-only package data once they are in the
    # wheel, which is what turns CLAUDE.md's "do not edit the tracked files
    # under group_vars" from documentation into packaging.
    assert f"{root}/inventory/group_vars/blitzecdn_edges/defaults.yml" in names
    assert f"{root}/ansible.cfg" in names
    assert f"{root}/requirements.yml" in names


def test_the_root_wheel_carries_the_image_build_inputs(
    wheels: dict[str, Path],
):
    """The Dockerfiles ship too, for the same reason the roles do.

    They were a top-level `docker/` directory that only a checkout has, and
    every consumer — the justfile, both integration scripts, the release
    workflow, the contract suites and the compose template the controlplane
    role renders — spelled that path again. An air-gapped fleet that has to
    build its own edge image on the controller had nothing to build from, and
    a wheel is the only artefact that reaches such a controller.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn/docker"
    assert f"{root}/edge/Dockerfile" in names
    # Not only the Dockerfile: the file it COPYs and probes with is part of
    # the context, and a build that shipped one without the other fails at
    # `nginx -t` in a layer nobody reads until the image is being published.
    assert f"{root}/edge/module-probe.conf" in names
    assert f"{root}/control-plane/Dockerfile" in names
    # BuildKit resolves this beside the Dockerfile, not at the context root, so
    # it travels in the wheel with it or the build context stops being pruned.
    assert f"{root}/control-plane/Dockerfile.dockerignore" in names


def test_core_locates_its_image_build_inputs_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """`blitzecdn.docker` answers from site-packages, like `blitzecdn.ansible`.

    Shipping the files is half of it; resolving them without counting `..`
    from `__file__` is the other half, and it is the half that fails silently
    in a checkout where the two answers coincide. Run from a working directory
    that is not the repository, and asserted to be under the virtualenv.
    """
    environment = _environment(tmp_path_factory.mktemp("core-docker") / "venv")
    environment.install(wheels["blitzecdn"])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn import docker;"
        "print(json.dumps({"
        "'context': str(docker.EDGE_CONTEXT),"
        "'exists': docker.EDGE_DOCKERFILE.is_file()"
        " and docker.EDGE_MODULE_PROBE_CONF.is_file()"
        " and docker.CONTROL_PLANE_DOCKERFILE.is_file()"
        " and docker.CONTROL_PLANE_DOCKERIGNORE.is_file(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert str(environment.root) in resolved["context"]
    assert str(REPO_ROOT) not in resolved["context"]


def test_the_root_wheel_publishes_no_control_plane_build_context(
    wheels: dict[str, Path],
):
    """`blitzecdn.docker` names that Dockerfile and stops there.

    Its build context is the distribution's own source — `pyproject.toml`,
    `uv.lock` and every workspace member under `packages/` — so a constant for
    it would be `project_dir` under another name, put back by the very module
    that removed the last one. The role supplies the context, and only until
    the control plane is delivered as a published image the way the edge is.
    """
    import ast
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        source = archive.read("blitzecdn/docker/__init__.py").decode("utf-8")

    module = ast.parse(source)
    # What is *defined*, not what is mentioned: the docstrings here explain the
    # absence, and a grep would forbid the explanation along with the thing.
    defined = {
        target.id
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    published = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    )

    assert "CONTROL_PLANE_CONTEXT" not in defined
    assert "CONTROL_PLANE_CONTEXT" not in published
    assert not any("CONTEXT" in name for name in published if "EDGE" not in name)


def test_core_locates_its_own_ansible_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """The installed root wheel finds its roles and plays with no checkout.

    The mirror of `test_an_installed_capability_locates_its_plays_without_the
    _repository`, and it fails for the same reason that one would if
    `blitzecdn_cache.ansible` counted `..` from `__file__`: core resolves its
    tree from `Settings.project_dir`, so the answer is a repository-relative
    path that is correct in a checkout and absent on a controller.

    Run from a working directory that is not the repository, and asserted to be
    under the virtualenv, so neither `cwd` nor a stray `BLITZE_PROJECT_DIR` can
    make it pass for the wrong reason.
    """
    environment = _environment(tmp_path_factory.mktemp("core-ansible") / "venv")
    environment.install(wheels["blitzecdn"])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn import ansible;"
        "print(json.dumps({"
        "'roles': str(ansible.ROLES_PATH),"
        "'edge': str(ansible.EDGE_PLAYBOOK),"
        "'names': sorted(p.name for p in ansible.ROLES_PATH.iterdir()"
        "  if p.is_dir()),"
        "'exists': ansible.EDGE_PLAYBOOK.is_file()"
        " and ansible.DECOMMISSION_PLAYBOOK.is_file()"
        " and ansible.ROLES_PATH.is_dir()"
        " and (ansible.INVENTORY_PLUGINS_PATH / 'blitzecdn.py').is_file(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert sorted(PLATFORM_ROLES) == resolved["names"]
    assert str(environment.root) in resolved["roles"]
    assert str(environment.root) in resolved["edge"]
    assert str(REPO_ROOT) not in resolved["roles"]
