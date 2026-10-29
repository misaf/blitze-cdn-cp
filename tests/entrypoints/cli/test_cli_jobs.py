"""`job list` and `job show`: what the queue holds and what a job reported."""

import json
from datetime import UTC, datetime

from support.cli import cli_control, runner

from blitzecdn.entrypoints.cli import main as cli


def test_job_show_prints_what_a_finished_operation_reported(settings, monkeypatch):
    """An operator on the host can read what an API client's operation found."""
    control = cli_control(settings, monkeypatch)
    submitted = control.queued_operations.submit("cache.stats", {"operator": "ops"})
    claim = control.job_queue.claim()
    assert claim is not None
    control.job_queue.jobs.complete(
        claim.job.id, claim.fence, now=datetime.now(UTC), result={"edges": []}
    )

    shown = runner.invoke(cli.app, ["job", "show", submitted.id, "--json"])

    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["result"] == {"edges": []}


def test_job_list_leaves_results_to_job_show(settings, monkeypatch):
    """A stats report per row would bury the queue it is listing."""
    control = cli_control(settings, monkeypatch)
    control.queued_operations.submit("cache.stats", {"operator": "ops"})

    listed = runner.invoke(cli.app, ["job", "list", "--json"])

    assert listed.exit_code == 0
    assert "result" not in json.loads(listed.stdout)[0]
