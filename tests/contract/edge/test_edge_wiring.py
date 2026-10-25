"""Repository and play wiring: pinned actions, named roles, the fixture."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from support.edge_render import (
    ROLES_DIR,
    _role_spec,
)
from support.paths import CORE_ANSIBLE, FIXTURES, REPO_ROOT, SOURCE

PROJECT_DIR = REPO_ROOT


FIXTURE = FIXTURES / "desired-state.yml"


def test_ci_actions_are_pinned_to_immutable_commits():
    for workflow in (PROJECT_DIR / ".github/workflows").glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" not in line:
                continue
            reference = line.split("uses:", 1)[1].strip().split()[0]
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                f"{workflow}: action is not pinned to a commit: {reference}"
            )


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _plays_and_their_roles() -> list[tuple[Path, tuple[Path, ...]]]:
    """Every play in the workspace, with the role directories it may resolve in.

    Core's plays see core's roles; a contributed play sees core's and its owner's,
    as `resolve_role_search_path` composes them.
    """
    found: list[tuple[Path, tuple[Path, ...]]] = [
        (playbook, (ROLES_DIR,))
        for playbook in sorted((CORE_ANSIBLE / "playbooks").glob("*.yml"))
    ]
    for tree in sorted(SOURCE.rglob("ansible")):
        if tree == CORE_ANSIBLE:
            continue
        search = (ROLES_DIR, tree / "roles") if (tree / "roles").is_dir() else ()
        found.extend(
            (playbook, search or (ROLES_DIR,))
            for playbook in sorted((tree / "playbooks").glob("*.yml"))
        )
    return found


def test_every_role_a_playbook_names_exists():
    """A role rename cannot leave a playbook pointing at a missing local role.

    A contributed play resolves against the same composed path a deployment uses.
    """
    referenced: set[str] = set()
    for playbook, search in _plays_and_their_roles():
        document = yaml.safe_load(playbook.read_text(encoding="utf-8"))
        for play in document:
            for entry in play.get("roles", []):
                name = entry["role"] if isinstance(entry, dict) else entry
                referenced.add(name)
                assert any((directory / name).is_dir() for directory in search), (
                    f"{playbook.name} names role {name}, which is in none of "
                    f"{', '.join(str(directory) for directory in search)}"
                )

    assert "blitzecdn_nginx" in referenced, "the sweep found no playbooks to check"
    assert "blitzecdn_cache_purge" in referenced, (
        "the sweep no longer reaches the plays an optional distribution owns"
    )


def test_no_reference_to_the_retired_edge_collection_remains():
    """A stale `blitzecdn.edge.` prefix resolves to nothing and fails at deploy."""
    tracked = [
        *sorted(CORE_ANSIBLE.rglob("*.yml")),
        *sorted(CORE_ANSIBLE.rglob("*.cfg")),
        PROJECT_DIR / "install.sh",
    ]
    offenders = [
        path.relative_to(PROJECT_DIR)
        for path in tracked
        if ".state" not in path.parts
        and "blitzecdn.edge" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"retired collection namespace still referenced in {offenders}"
    )


# The SSH drop-in's own assertions are in `tests/capabilities/hardening/`.
# `ansible.cfg` is the control plane's file, so this half stays.
def test_controller_refuses_password_authentication():
    """The other half of the contract: what this repository dials out with."""
    config = (CORE_ANSIBLE / "ansible.cfg").read_text(encoding="utf-8")
    for option in (
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "BatchMode=yes",
    ):
        assert option in config, (
            f"the platform ansible.cfg no longer passes -o {option}, so a "
            "deploy could authenticate to an edge with a password."
        )
    assert "host_key_checking = True" in config


def test_empty_site_removal_requires_explicit_approval(desired_state):
    assert desired_state["blitzecdn_nginx_allow_empty_sites"] is False
    assert "blitzecdn_nginx_allow_empty_sites" in _role_spec()


def test_committed_fixture_matches_generated_desired_state(desired_state):
    """CI feeds this fixture to a real playbook; keep it honest.

    Set BLITZECDN_UPDATE_FIXTURE=1 to rewrite it after an intentional change.
    """
    if os.environ.get("BLITZECDN_UPDATE_FIXTURE"):
        FIXTURE.write_text(
            "---\n" + yaml.dump(desired_state, Dumper=_IndentedDumper, sort_keys=False),
            encoding="utf-8",
        )
    committed = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    expected = dict(committed)
    actual = dict(desired_state)
    # The fixture is an Ansible example, not a compiled release: capability
    # availability is part of release identity, so envelope digests differ by
    # installation.
    for document in (expected, actual):
        assert re.fullmatch(r"[0-9a-f]{64}", document.pop("blitzecdn_release_digest"))
    assert expected == actual, (
        f"{FIXTURE} is stale. Regenerate it with:\n"
        "  BLITZECDN_UPDATE_FIXTURE=1 just test-one "
        "tests/contract/edge/test_edge_wiring.py::"
        "test_committed_fixture_matches_generated_desired_state"
    )
