"""Benchmark batched rollouts across fleet and batch sizes (`just bench-rollout`).

Drives the shipped :class:`Rollout` and the real SQLite deployment store against a
*simulated* Ansible runner and serving verifier. Every playbook invocation, phase,
canary, batch split, failure, withdrawal and drift-check yield is the production
code path; only the time an ``ansible-playbook`` run and an HTTP probe would take
is modelled, on a virtual clock, from the cost model printed with the results.

So two kinds of number come out, and the report keeps them apart:

- **Measured** on this machine: playbook invocation counts, batch shapes, and the
  controller's own CPU time and peak Python allocation for the orchestration
  (database writes, result parsing). Ansible's own process is not run, so its
  CPU and memory are not in these figures.
- **Simulated**: rollout, phase and canary durations, and drift-check yield
  latency. They are only as good as the cost model; real disposable edges are
  needed to calibrate it (docs/design/rollout-benchmark.md).

    uv run python scripts/rollout_benchmark.py [--fleet 10,50,200] [--batch 1,10]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import random
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentStore,
    DeploymentTargetStore,
)
from blitzecdn.capabilities.deployments.domain import TargetStatus
from blitzecdn.capabilities.deployments.service.rollout import (
    DEFAULT_BATCH_SIZE,
    Rollout,
    RolloutOutcome,
)
from blitzecdn.capabilities.releases import Release
from blitzecdn.capabilities.releases.adapters.persistence import ReleaseStore
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.releases.service import compile_release
from blitzecdn.capabilities.releases.service.preparation import prepare_compilation
from blitzecdn.capabilities.sites.domain import DnsRecord, Domain
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import (
    AnsibleRun,
    HostRun,
    RunStatus,
    TaskOutcome,
    TaskResult,
)
from blitzecdn.core.persistence.schema import DatabaseSchema

#: How soon `blitzecdn deploy --wait` retries a busy lock (deployments/cli.py).
CLI_WAIT_POLL_SECONDS = 2.0
#: Where in a drift check the competing deployment arrives, as fractions of it.
YIELD_ARRIVALS = (0.25, 0.5, 0.75)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CostModel:
    """What one ``ansible-playbook`` run and one serving probe are assumed to cost.

    Defaults are estimates, not measurements: replace them with figures from
    disposable edges before reading the simulated durations as predictions.
    """

    #: Process start, inventory plugin, collection import, SSH multiplexing.
    startup: float = 5.0
    #: ``gather_facts`` on one host, paid in every run.
    facts: float = 2.0
    #: Per-host task time for each tag selection, after facts.
    validate: float = 15.0
    stage: float = 20.0
    activate: float = 4.0
    commit: float = 2.0
    rollback: float = 4.0
    #: One external serving probe, which the controller makes edge by edge.
    verify: float = 0.5
    #: Lognormal spread of a host's time; a run waits for its slowest host.
    jitter: float = 0.3
    #: ``forks`` in ansible.cfg: hosts one run works on at once.
    forks: int = 20

    def phase(self, label: str) -> float:
        return float(getattr(self, label))


def phase_label(*, check: bool, tags: tuple[str, ...]) -> str:
    if check or not tags:
        return "validate"
    return tags[0]


class VirtualClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> datetime:
        return _EPOCH + timedelta(seconds=self.seconds)


@dataclass
class Ledger:
    """Everything the simulated edges were asked to do, and how long it took."""

    invocations: Counter[str] = field(default_factory=Counter)
    phase_seconds: defaultdict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    batch_sizes: list[int] = field(default_factory=list)
    #: Virtual time the canary's commit finished, for a real rollout.
    canary_done: float | None = None


class SimulatedRunner:
    """Answers runs per host like Runner's events, advancing the virtual clock.

    ``failing`` maps a phase label to the edges that fail it. Under the play's
    ``any_errors_fatal`` the other hosts in that run still report, unfailed.
    """

    def __init__(
        self,
        clock: VirtualClock,
        model: CostModel,
        ledger: Ledger,
        *,
        failing: dict[str, set[str]],
        seed: int,
    ) -> None:
        self.clock = clock
        self.model = model
        self.ledger = ledger
        self.failing = failing
        self.rng = random.Random(seed)  # noqa: S311 - reproducible model, not secrets

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self, variables: Path) -> AnsibleRun:
        raise NotImplementedError

    def run(
        self, *, check: bool, host_limit: str | None = None, tags: tuple[str, ...] = ()
    ) -> AnsibleRun:
        label = phase_label(check=check, tags=tags)
        edges = (host_limit or "").split(",")
        started = self.clock()
        duration = self.model.startup + self._makespan(label, len(edges))
        self.clock.seconds += duration
        self.ledger.invocations[label] += 1
        self.ledger.phase_seconds[label] += duration
        if label == "validate":
            self.ledger.batch_sizes.append(len(edges))
        if label == "commit" and self.ledger.canary_done is None:
            self.ledger.canary_done = self.clock.seconds
        bad = self.failing.get(label, set()) & set(edges)
        return AnsibleRun(
            id=f"run-{sum(self.ledger.invocations.values())}",
            playbook="edge.yml",
            status=RunStatus.FAILED if bad else RunStatus.SUCCEEDED,
            return_code=2 if bad else 0,
            started_at=started,
            finished_at=self.clock(),
            hosts=tuple(_host(edge, failed=edge in bad) for edge in edges),
            targeted=tuple(edges),
        )

    def _makespan(self, label: str, hosts: int) -> float:
        """Waves of ``forks`` hosts; each wave lasts as long as its slowest host."""
        per_host = self.model.facts + self.model.phase(label)
        total = 0.0
        for wave in range(math.ceil(hosts / self.model.forks)):
            width = min(self.model.forks, hosts - wave * self.model.forks)
            total += max(
                per_host * self.rng.lognormvariate(0, self.model.jitter)
                for _ in range(width)
            )
        return total


def _host(edge: str, *, failed: bool) -> HostRun:
    return HostRun(
        host=edge,
        ok=4,
        failed=1 if failed else 0,
        failures=(
            (
                TaskResult(
                    task="simulated", outcome=TaskOutcome.FAILED, message="injected"
                ),
            )
            if failed
            else ()
        ),
    )


@dataclass(frozen=True)
class _Probe:
    edge: str
    hostname: str
    served: bool
    detail: str


class SimulatedVerifier:
    def __init__(
        self, clock: VirtualClock, model: CostModel, ledger: Ledger, failing: set[str]
    ) -> None:
        self.clock = clock
        self.model = model
        self.ledger = ledger
        self.failing = failing

    def verify(
        self, *, edge: str, address: str, sites: Sequence[Any], release_digest: str
    ) -> list[_Probe]:
        self.clock.seconds += self.model.verify
        self.ledger.phase_seconds["verify"] += self.model.verify
        served = edge not in self.failing
        return [
            _Probe(edge, site.server_names[0], served, "200" if served else "refused")
            for site in sites
            if site.server_names
        ]


class Addresses:
    def __init__(self, edges: Sequence[str]) -> None:
        self.edges = set(edges)

    def selected(self, host_limit: str | None) -> list[str]:
        return sorted(self.edges) if host_limit is None else host_limit.split(",")

    def public_address(self, edge: str) -> str | None:
        return "192.0.2.10" if edge in self.edges else None


class NoContributions:
    def site_variables(self, site: Any) -> dict[str, Any]:
        return {"name": site.name}

    def fleet_variables(self, sites: Any) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class Scenario:
    name: str
    check: bool = False
    #: Phase label -> index of the edge that fails it.
    failure: tuple[str, int] | None = None
    #: Fraction of the plain drift check after which a deployment wants the lock.
    contention_at: float | None = None


@dataclass
class Result:
    scenario: str
    fleet: int
    batch: int
    name_length: int
    batches: int
    largest_batch: int
    invocations: int
    by_phase: dict[str, int]
    rollbacks: int
    duration_s: float
    phase_s: dict[str, float]
    canary_s: float | None
    outcome: dict[str, int]
    yielded: bool
    lock_wait_s: float | None
    controller_cpu_ms: float
    controller_wall_ms: float
    controller_peak_kib: float | None


class Bench:
    def __init__(self, directory: Path, model: CostModel, seed: int) -> None:
        database = directory / "control-plane.db"
        DatabaseSchema.migrate(database)
        self.repository = Repository(database)
        self.deployments = DeploymentStore(self.repository.database)
        self.targets = DeploymentTargetStore(self.repository.database)
        self.model = model
        self.seed = seed
        #: Uncontended drift-check duration per (fleet, batch, name length).
        self.drift: dict[tuple[int, int, int], float] = {}
        inputs = ReleaseInputs.of(
            [Domain(name="example.com")],
            [DnsRecord(domain="example.com", name="cdn", value="198.51.100.10")],
            [],
        )
        self.release: Release = compile_release(
            prepare_compilation(
                inputs,
                capabilities=("cache", "compression"),
                targets=(),
                contributors=NoContributions(),
                site_checks=lambda site: (),
                allow_empty_sites=True,
            )
        )
        with self.repository.transaction():
            ReleaseStore(self.repository.database, self.deployments).save(
                self.release, inputs
            )

    def execute(
        self,
        scenario: Scenario,
        edges: Sequence[str],
        batch: int,
        *,
        arrival: float | None = None,
        trace: bool = False,
    ) -> tuple[RolloutOutcome, Ledger, VirtualClock, float, float, float]:
        clock = VirtualClock()
        ledger = Ledger()
        failing: dict[str, set[str]] = {}
        if scenario.failure is not None:
            label, index = scenario.failure
            failing[label] = {edges[min(index, len(edges) - 1)]}
        runner = SimulatedRunner(
            clock, self.model, ledger, failing=failing, seed=self.seed
        )
        rollout = Rollout(
            targets=self.targets,
            runner=runner,
            addresses=Addresses(edges),
            verifier=SimulatedVerifier(
                clock, self.model, ledger, failing.get("verify", set())
            ),
            publish=lambda _release: None,
            clock=clock,
            batch_size=batch,
        )
        should_yield: Callable[[], bool] | None = None
        if arrival is not None:
            should_yield = lambda: clock.seconds >= arrival  # noqa: E731
        with self.repository.transaction():
            deployment = self.deployments.create_deployment(
                "benchmark", release_id=self.release.id, check_mode=scenario.check
            ).id
        if trace:
            tracemalloc.start()
        cpu, wall = time.process_time(), time.perf_counter()
        outcome = rollout.converge(
            deployment,
            self.release,
            edges=edges,
            check=scenario.check,
            should_yield=should_yield,
        )
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        peak = 0.0
        if trace:
            peak = tracemalloc.get_traced_memory()[1] / 1024
            tracemalloc.stop()
        return outcome, ledger, clock, cpu, wall, peak

    def measure(
        self, scenario: Scenario, fleet: int, batch: int, name_length: int
    ) -> Result:
        edges = _names(fleet, name_length)
        key = (fleet, batch, name_length)
        arrival = None
        if scenario.contention_at is not None:
            # Arrive a fraction of the way through the uncontended check.
            if key not in self.drift:
                _, _, plain, *_ = self.execute(
                    Scenario("drift", check=True), edges, batch
                )
                self.drift[key] = plain.seconds
            arrival = self.drift[key] * scenario.contention_at
        outcome, ledger, clock, cpu, wall, _ = self.execute(
            scenario, edges, batch, arrival=arrival
        )
        if scenario.check and arrival is None:
            self.drift[key] = clock.seconds
        peak = None
        if scenario.name in _TRACED:
            # A second pass: tracing slows the first one's CPU figure.
            *_, peak = self.execute(scenario, edges, batch, trace=True)
        statuses = Counter(target.status.value for target in outcome.targets)
        return Result(
            scenario=scenario.name,
            fleet=fleet,
            batch=batch,
            name_length=name_length,
            batches=len(ledger.batch_sizes),
            largest_batch=max(ledger.batch_sizes, default=0),
            invocations=sum(ledger.invocations.values()),
            by_phase=dict(ledger.invocations),
            rollbacks=ledger.invocations["rollback"],
            duration_s=round(clock.seconds, 1),
            phase_s={k: round(v, 1) for k, v in ledger.phase_seconds.items()},
            canary_s=(
                None if ledger.canary_done is None else round(ledger.canary_done, 1)
            ),
            outcome={s.value: statuses.get(s.value, 0) for s in TargetStatus},
            yielded=outcome.yielded,
            # Until the check lets go of the lock: by standing aside at the next
            # batch boundary, or by finishing when the arrival was in its last batch.
            lock_wait_s=None if arrival is None else round(clock.seconds - arrival, 1),
            controller_cpu_ms=round(cpu * 1000, 1),
            controller_wall_ms=round(wall * 1000, 1),
            controller_peak_kib=None if peak is None else round(peak, 1),
        )


def _names(count: int, length: int) -> list[str]:
    """Edge names exactly ``length`` characters long, so host-limit splits show."""
    return [f"edge-{index:04d}".ljust(length, "x") for index in range(count)]


#: Scenarios whose peak Python allocation is traced in a second pass.
_TRACED = ("rollout", "drift-check")


def scenarios(batch: int) -> list[Scenario]:
    # The edge that fails sits mid-way into the first full batch after the canary.
    victim = 1 + batch // 2
    return [
        Scenario("rollout"),
        Scenario("fail-stage", failure=("stage", victim)),
        Scenario("fail-activate", failure=("activate", victim)),
        Scenario("fail-verify", failure=("verify", victim)),
        Scenario("drift-check", check=True),
        *(
            Scenario(f"drift-yield@{int(at * 100)}%", check=True, contention_at=at)
            for at in YIELD_ARRIVALS
        ),
    ]


def environment(model: CostModel, arguments: argparse.Namespace) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        commit = "unknown"
    return {
        "kind": "simulated runner and verifier; real Rollout and SQLite store",
        "command": " ".join([Path(sys.executable).name, *sys.argv]),
        "commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "default_batch_size": DEFAULT_BATCH_SIZE,
        "seed": arguments.seed,
        "cost_model": asdict(model),
        "yield_waiter_poll_s": CLI_WAIT_POLL_SECONDS,
    }


def report(env: dict[str, Any], results: Sequence[Result]) -> None:
    print("# Rollout benchmark\n")
    for key, value in env.items():
        print(f"- {key}: {value}")
    print(
        "\nDurations, canary and yield latency are SIMULATED from the cost model. "
        "Invocations, batches and controller CPU/peak are MEASURED.\n"
    )
    header = (
        "| scenario | N | B | name len | batches (max) | runs | rollbacks "
        "| sim total s | sim canary s | sim lock wait s | outcome S/F/K/P "
        "| ctl cpu ms | ctl wall ms | ctl peak KiB |"
    )
    print(header)
    print("|" + "---|" * (header.count("|") - 1))
    for r in results:
        o = r.outcome
        print(
            f"| {r.scenario}{' (yielded)' if r.yielded else ''} "
            f"| {r.fleet} | {r.batch} | {r.name_length} "
            f"| {r.batches} ({r.largest_batch}) | {r.invocations} | {r.rollbacks} "
            f"| {r.duration_s} | {r.canary_s if r.canary_s is not None else '-'} "
            f"| {r.lock_wait_s if r.lock_wait_s is not None else '-'} "
            f"| {o['succeeded']}/{o['failed']}/{o['skipped']}/{o['pending']} "
            f"| {r.controller_cpu_ms} | {r.controller_wall_ms} "
            f"| {'-' if r.controller_peak_kib is None else r.controller_peak_kib} |"
        )
    rollouts = [r for r in results if r.scenario == "rollout"]
    if rollouts:
        print("\n## Simulated seconds per phase, full rollout\n")
        phases = ("validate", "stage", "activate", "verify", "commit")
        print("| N | B | name len | " + " | ".join(phases) + " |")
        print("|---|---|---|" + "---|" * len(phases))
        for r in rollouts:
            print(
                f"| {r.fleet} | {r.batch} | {r.name_length} | "
                + " | ".join(str(r.phase_s.get(p, 0.0)) for p in phases)
                + " |"
            )
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux KiB.
    rss_mib = rss / 2**20 if sys.platform == "darwin" else rss / 1024
    print(f"\nController process peak RSS over the whole benchmark: {rss_mib:.1f} MiB")


@dataclass(frozen=True)
class Invocation:
    """One line of `scripts/ansible-playbook-timing.sh`'s log."""

    start: float
    wall: float
    cpu: float
    rss_kib: int
    failed: bool
    phase: str
    hosts: int

    @classmethod
    def parse(cls, line: str) -> Invocation:
        start, wall, user, system, rss, rc, check, tags, limit = line.split("\t")
        return cls(
            start=float(start),
            wall=float(wall),
            cpu=float(user) + float(system),
            rss_kib=int(rss),
            failed=rc != "0",
            phase=phase_label(
                check=check == "1",
                tags=() if tags == "-" else tuple(tags.split(",")),
            ),
            hosts=0 if limit == "-" else len(limit.split(",")),
        )


def summarize(log: Path) -> None:
    """Report a real rollout from `scripts/ansible-playbook-timing.sh`'s log."""
    rows = [
        Invocation.parse(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not rows:
        raise SystemExit(f"{log} holds no invocations")
    span = max(r.start + r.wall for r in rows) - min(r.start for r in rows)
    print(f"# Measured on real edges: {log}\n")
    print(f"{len(rows)} ansible-playbook invocations over {span:.1f} s\n")
    print(
        "| phase | runs | failed | hosts/run (max) | wall s total | wall s mean "
        "| wall s max | ansible cpu s | max rss MiB |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for phase in ("validate", "stage", "activate", "commit", "rollback"):
        group = [r for r in rows if r.phase == phase]
        if not group:
            continue
        walls = [r.wall for r in group]
        hosts = [r.hosts for r in group]
        print(
            f"| {phase} | {len(group)} | {sum(r.failed for r in group)} "
            f"| {sum(hosts) / len(hosts):.1f} ({max(hosts)}) | {sum(walls):.1f} "
            f"| {sum(walls) / len(walls):.1f} | {max(walls):.1f} "
            f"| {sum(r.cpu for r in group):.1f} "
            f"| {max(r.rss_kib for r in group) / 1024:.0f} |"
        )


def _ints(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part]


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--fleet", type=_ints, default=[10, 50, 200])
    parser.add_argument("--batch", type=_ints, default=[1, 5, 10, 20, 50])
    parser.add_argument(
        "--name-length",
        type=_ints,
        default=[12, 60],
        help="edge name lengths; 60 makes the 512-character host limit split batches",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", type=Path, help="also write every result here")
    parser.add_argument(
        "--summarize",
        type=Path,
        metavar="LOG",
        help="instead, report a real rollout from ansible-playbook-timing.sh's log",
    )
    for name, value in asdict(CostModel()).items():
        parser.add_argument(
            f"--{name.replace('_', '-')}", type=type(value), default=value
        )
    arguments = parser.parse_args()
    if arguments.summarize is not None:
        summarize(arguments.summarize)
        return
    # Injected failures are expected; their tracebacks would bury the report.
    logging.getLogger("blitzecdn").setLevel(logging.CRITICAL)
    model = CostModel(**{k: getattr(arguments, k) for k in asdict(CostModel())})
    results: list[Result] = []
    with tempfile.TemporaryDirectory() as raw:
        bench = Bench(Path(raw), model, arguments.seed)
        try:
            for length in arguments.name_length:
                for fleet in arguments.fleet:
                    for batch in arguments.batch:
                        # Long names only matter where they split a batch.
                        if length != arguments.name_length[0] and batch < 10:
                            continue
                        for scenario in scenarios(batch):
                            if length != arguments.name_length[
                                0
                            ] and scenario.name not in (
                                "rollout",
                                "drift-check",
                            ):
                                continue
                            results.append(
                                bench.measure(scenario, fleet, batch, length)
                            )
        finally:
            bench.repository.close()
    env = environment(model, arguments)
    report(env, results)
    if arguments.json:
        arguments.json.write_text(
            json.dumps(
                {"environment": env, "results": [asdict(r) for r in results]}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
