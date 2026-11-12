"""`scripts/rollout_benchmark.py` keeps measuring what the docs say it measures.

The benchmark drives the real rollout. These cases pin the facts its report
rests on: the run-count formula in docs/design/edge-playbook.md, the host-limit
split, the failure policy, and that a contended drift check lets go of the lock
at a batch boundary.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest
from support.paths import REPO_ROOT


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "rollout_benchmark.py"
    spec = importlib.util.spec_from_file_location("rollout_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module by name while the class is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench_module = _load()


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory):
    directory: Path = tmp_path_factory.mktemp("rollout-bench")
    bench = bench_module.Bench(directory, bench_module.CostModel(), seed=1)
    yield bench
    bench.repository.close()


@pytest.mark.parametrize(("fleet", "batch"), [(1, 10), (7, 1), (7, 3), (12, 5)])
def test_a_rollout_makes_the_documented_number_of_runs(bench, fleet, batch):
    result = bench.measure(bench_module.Scenario("rollout"), fleet, batch, 12)

    assert result.invocations == 4 * (1 + math.ceil((fleet - 1) / batch))
    assert result.outcome["succeeded"] == fleet
    assert result.canary_s is not None and result.canary_s <= result.duration_s


def test_a_drift_check_needs_no_canary(bench):
    result = bench.measure(bench_module.Scenario("drift-check", check=True), 12, 5, 12)

    assert result.invocations == math.ceil(12 / 5)
    assert result.largest_batch == 5


def test_long_names_split_a_batch_at_the_host_limit(bench):
    # 60-character names: eight fit in 512 characters with their commas.
    result = bench.measure(bench_module.Scenario("drift-check", check=True), 10, 10, 60)

    assert result.batches == 2
    assert result.largest_batch == 8


def test_an_activation_failure_withdraws_its_whole_batch(bench):
    scenario = bench_module.scenarios(4)[2]
    assert scenario.name == "fail-activate"

    result = bench.measure(scenario, 9, 4, 12)

    # The canary stays; the next batch of four is withdrawn; the rest never start.
    assert (
        result.outcome["succeeded"],
        result.outcome["failed"],
        result.outcome["skipped"],
    ) == (1, 4, 4)
    assert result.rollbacks == 4


def test_a_contended_drift_check_lets_go_at_the_next_batch(bench):
    plain = bench.measure(bench_module.Scenario("drift-check", check=True), 9, 3, 12)
    contended = bench.measure(
        bench_module.Scenario("drift-yield", check=True, contention_at=0.5), 9, 3, 12
    )

    assert contended.yielded
    assert contended.invocations < plain.invocations
    assert contended.lock_wait_s is not None
    assert 0 < contended.lock_wait_s < plain.duration_s


def test_a_real_rollouts_timing_log_is_read_per_phase(tmp_path, capsys):
    log = tmp_path / "timings.tsv"
    log.write_text(
        "1.0\t30.5\t9.0\t1.0\t204800\t0\t1\t-\tedge-a\n"
        "32.0\t40.0\t8.0\t2.0\t102400\t0\t0\tstage\tedge-b,edge-c\n"
        "73.0\t5.0\t1.0\t0.5\t51200\t2\t0\tactivate\tedge-b,edge-c\n",
        encoding="utf-8",
    )

    bench_module.summarize(log)

    report = capsys.readouterr().out
    assert "3 ansible-playbook invocations over 77.0 s" in report
    assert "| validate | 1 | 0 | 1.0 (1) | 30.5 |" in report
    assert "| stage | 1 | 0 | 2.0 (2) | 40.0 |" in report
    assert "| activate | 1 | 1 |" in report
