"""The callback plugin, driven the way Ansible drives it.

This is the seam that replaced `PLAY RECAP` parsing, so it is worth exercising
directly: the runner tests feed a hand-written document to prove the reader is
right, and these prove the writer produces that shape from the objects Ansible
actually hands a callback.

The plugin is loaded from `ansible/plugins/callback/` by path, because that is
where Ansible finds it too — importing a copy would let the two drift.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from blitzecdn.domain.runs import HostRun

PLUGIN = (
    Path(__file__).resolve().parent.parent
    / "ansible/plugins/callback/blitzecdn_result.py"
)

pytest.importorskip("ansible.plugins.callback")


def _load():
    spec = importlib.util.spec_from_file_location("blitzecdn_result", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load()


class FakeHost:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class FakeTask:
    def __init__(self, name: str, action: str = "ansible.builtin.template") -> None:
        self._name = name
        self.action = action
        self._role = None

    def get_name(self) -> str:
        return self._name


class FakeResult:
    """What Ansible passes a v2_runner_on_* hook."""

    def __init__(self, host: str, task: FakeTask, **fields: object) -> None:
        self._host = FakeHost(host)
        self._task = task
        self._result = fields


class FakeStats:
    def __init__(self, summaries: dict[str, dict[str, int]]) -> None:
        self.processed = dict.fromkeys(summaries, 1)
        self._summaries = summaries

    def summarize(self, host: str) -> dict[str, int]:
        return self._summaries[host]


@pytest.fixture
def callback(tmp_path, monkeypatch):
    """A plugin instance pointed at a result path of its own."""
    monkeypatch.setenv("BLITZE_RESULT_PATH", str(tmp_path / "result.json"))
    return MODULE.CallbackModule()


def _document(callback) -> dict:
    return json.loads(Path(callback._path).read_text(encoding="utf-8"))


def test_it_records_which_tasks_changed_not_only_how_many(callback):
    """The whole reason for a callback: a recap could only ever give a count."""
    task = FakeTask("Render managed sites")
    callback.v2_runner_on_ok(FakeResult("edge-a", task, changed=True))
    callback.v2_runner_on_ok(
        FakeResult("edge-a", FakeTask("Reload Nginx"), changed=True)
    )
    callback.v2_runner_on_ok(FakeResult("edge-b", task, changed=False))
    callback.v2_playbook_on_stats(
        FakeStats(
            {
                "edge-a": {"ok": 9, "changed": 2},
                "edge-b": {"ok": 9, "changed": 0},
            }
        )
    )

    hosts = {host["host"]: host for host in _document(callback)["hosts"]}
    assert [change["task"] for change in hosts["edge-a"]["changes"]] == [
        "Render managed sites",
        "Reload Nginx",
    ]
    assert hosts["edge-a"]["changes"][0]["action"] == "ansible.builtin.template"
    assert hosts["edge-b"]["changes"] == []


def test_a_failure_carries_the_message_ansible_produced(callback):
    callback.v2_runner_on_failed(
        FakeResult(
            "edge-a",
            FakeTask("Validate the assembled configuration"),
            msg="nginx: configuration file /etc/nginx/nginx.conf test failed",
        )
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-a": {"ok": 3, "failures": 1}}))

    failure = _document(callback)["hosts"][0]["failures"][0]
    assert failure["task"] == "Validate the assembled configuration"
    assert "test failed" in failure["message"]
    assert failure["outcome"] == "failed"


def test_an_ignored_error_is_not_a_failure(callback):
    """`ignore_errors` is the play choosing to continue, not a failed host."""
    callback.v2_runner_on_failed(
        FakeResult("edge-a", FakeTask("Best effort"), msg="nope"), ignore_errors=True
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-a": {"ok": 3, "ignored": 1}}))

    host = _document(callback)["hosts"][0]
    assert host["failures"] == []
    assert host["ignored"] == 1


def test_an_unreachable_host_is_recorded_as_such(callback):
    callback.v2_runner_on_unreachable(
        FakeResult("edge-c", FakeTask("Gathering Facts"), msg="No route to host")
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-c": {"unreachable": 1}}))

    host = _document(callback)["hosts"][0]
    assert host["unreachable"] == 1
    assert host["failures"][0]["outcome"] == "unreachable"


def test_ansible_own_tallies_win_over_what_the_hooks_saw(callback):
    """A rescued block never reaches the per-task hooks as a rescue.

    The counters therefore come from `stats.summarize`, which accounts for
    rescues and ignored errors; the task lists are what the summary cannot
    provide.
    """
    callback.v2_runner_on_failed(FakeResult("edge-a", FakeTask("Render"), msg="bad"))
    callback.v2_playbook_on_stats(
        FakeStats({"edge-a": {"ok": 5, "failures": 0, "rescued": 1}})
    )

    host = _document(callback)["hosts"][0]
    assert host["failed"] == 0
    assert host["rescued"] == 1


def test_a_role_payload_is_collected_from_the_report_fact(callback):
    callback.v2_runner_on_ok(
        FakeResult(
            "edge-a",
            FakeTask("Publish this edge's report", action="ansible.builtin.set_fact"),
            ansible_facts={MODULE.REPORT_FACT: {"nginx_reachable": True}},
        )
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-a": {"ok": 1}}))

    assert _document(callback)["hosts"][0]["report"] == {"nginx_reachable": True}


def test_an_unrelated_fact_is_not_collected(callback):
    """Only the agreed name is a report; everything else is the play's business."""
    callback.v2_runner_on_ok(
        FakeResult(
            "edge-a",
            FakeTask("Set something", action="ansible.builtin.set_fact"),
            ansible_facts={"blitzecdn_stats_counts": {"noise": 1}},
        )
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-a": {"ok": 1}}))

    assert _document(callback)["hosts"][0]["report"] is None


def test_the_document_validates_against_the_domain_model(callback):
    """Both halves of the contract, checked against each other.

    The runner parses this document into `HostRun`. If the plugin grows a field
    the model forbids, or drops one it requires, this is where the two stop
    agreeing — the alternative is finding out during a deploy.
    """
    callback.v2_runner_on_ok(FakeResult("edge-a", FakeTask("Render"), changed=True))
    callback.v2_runner_on_failed(FakeResult("edge-b", FakeTask("Reload"), msg="boom"))
    callback.v2_playbook_on_stats(
        FakeStats(
            {
                "edge-a": {"ok": 9, "changed": 1, "skipped": 2},
                "edge-b": {"ok": 4, "failures": 1},
            }
        )
    )

    hosts = [HostRun.model_validate(host) for host in _document(callback)["hosts"]]
    assert [host.host for host in hosts] == ["edge-a", "edge-b"]
    assert hosts[0].in_sync is False
    assert hosts[1].succeeded is False


def test_it_writes_nothing_when_no_result_path_is_set(tmp_path, monkeypatch):
    """An operator running ansible-playbook by hand gets ordinary behaviour.

    No stray files, and no exception out of a callback either — an exception
    here would surface as a warning on every task of a hand-run playbook.
    """
    monkeypatch.delenv("BLITZE_RESULT_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    instance = MODULE.CallbackModule()
    assert instance._path is None

    instance.v2_runner_on_ok(FakeResult("edge-a", FakeTask("Render"), changed=True))
    instance.v2_playbook_on_stats(FakeStats({"edge-a": {"ok": 1}}))

    assert list(tmp_path.iterdir()) == []


def test_a_long_failure_message_is_bounded(callback):
    """Module output belongs in the log; the document has to stay readable."""
    callback.v2_runner_on_failed(
        FakeResult("edge-a", FakeTask("Render"), msg="x" * 10_000)
    )
    callback.v2_playbook_on_stats(FakeStats({"edge-a": {"failures": 1}}))

    message = _document(callback)["hosts"][0]["failures"][0]["message"]
    assert len(message) == MODULE._MAX_MESSAGE
