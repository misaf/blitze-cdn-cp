from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

from blitzecdn.config import Settings
from blitzecdn.infrastructure.ansible import CommandResult


class FakeRunner:
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.results = results or [CommandResult(0, "ok", "")]
        self.check_modes: list[bool] = []

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self) -> CommandResult:
        return self.results[0]

    def run(self, *, check: bool) -> CommandResult:
        self.check_modes.append(check)
        return self.results.pop(0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ansible = tmp_path / "ansible"
    (ansible / "inventory").mkdir(parents=True)
    (ansible / "playbooks").mkdir()
    (ansible / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    inventory = ansible / "inventory/hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts: {}\n",
        encoding="utf-8",
    )
    playbook = ansible / "playbooks/edge.yml"
    playbook.write_text("- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8")
    state = tmp_path / "state"
    return Settings(
        project_dir=tmp_path,
        state_dir=state,
        database_path=state / "control-plane.db",
        ansible_dir=ansible,
        inventory_path=inventory,
        playbook_path=playbook,
        generated_vars_path=state / "desired-state.yml",
        deployment_lock_path=state / "deployment.lock",
        ansible_playbook="/usr/bin/true",
        api_keys={"tester": "x" * 32},
    )


@pytest.fixture
def site_payload() -> dict[str, object]:
    return {
        "name": "example-cdn",
        "server_names": ["cdn.example.com"],
        "origin_host": "origin.example.com",
    }
