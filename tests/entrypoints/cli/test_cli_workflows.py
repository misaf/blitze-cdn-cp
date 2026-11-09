"""`workflow list`, `show` and `acknowledge`: finding and signing off on work."""

import json

from support.cli import cli_control, runner

from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.entrypoints.cli import main as cli


def test_workflow_acknowledge_moves_needs_review_to_reviewed(settings, monkeypatch):
    control = cli_control(settings, monkeypatch)
    workflows = control.repository.workflows
    workflows.create("interrupted", "certificate", "alice", "example-com")
    workflows.advance(
        "interrupted", WorkflowStatus.NEEDS_REVIEW, error="restarted mid-order"
    )

    listed = runner.invoke(cli.app, ["workflow", "list", "--json"])
    acknowledged = runner.invoke(
        cli.app, ["workflow", "acknowledge", "interrupted", "--json"]
    )
    shown = runner.invoke(cli.app, ["workflow", "show", "interrupted", "--json"])
    again = runner.invoke(cli.app, ["workflow", "acknowledge", "interrupted"])

    assert json.loads(listed.stdout)[0]["status"] == "needs_review"
    assert acknowledged.exit_code == 0
    assert json.loads(acknowledged.stdout)["status"] == "reviewed"
    document = json.loads(shown.stdout)
    assert document["error"] == "restarted mid-order"
    assert document["steps"][-1]["details"] == {"operator": "cli"}
    assert again.exit_code != 0
