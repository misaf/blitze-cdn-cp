# Rollout benchmark

Code: `scripts/rollout_benchmark.py` (`just bench-rollout`),
`scripts/ansible-playbook-timing.sh`.

What `deployment_batch_size` trades, measured where it can be and modelled where
it cannot. [Edge playbook](edge-playbook.md#how-a-rollout-is-paced) describes the
pacing being measured: canary first, then batches of B.

## What is measured and what is simulated

The benchmark drives the shipped `Rollout` and the real SQLite deployment store.
The canary, batches, host-limit splits, failure handling, withdrawals and
drift-check yields are all the production code. Only `ansible-playbook` and the
serving probe are stand-ins. They advance a virtual clock according to a cost
model instead of contacting edges.

| Figure | Kind | Where it comes from |
| --- | --- | --- |
| Playbook invocations per phase, batches, largest batch | **Measured** | The real `Rollout` |
| Edge outcomes (succeeded / failed / skipped) | **Measured** | The real store |
| Controller CPU, wall time, peak Python allocation | **Measured** | `process_time`, `perf_counter`, `tracemalloc` around `converge` |
| Total, per-phase and canary durations | **Simulated** | Cost model below |
| How long a deployment waits for a drift check's lock | **Simulated** | Cost model, plus the real yield check |

The simulated durations predict nothing until the cost model has been calibrated
against [real disposable edges](#measuring-on-disposable-edges). The controller
figures leave out Ansible's own process, which the benchmark never starts.

### Cost model

Each run costs `startup + Σ waves`. A wave is up to `forks` hosts, and it lasts
as long as its slowest host: `(facts + phase) × lognormal(0, jitter)`. This is
roughly how the linear strategy behaves when `forks` limits it. The controller
probes edges one at a time during verification.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `startup` | 5 s | Process start, inventory plugin, collections, SSH multiplexing |
| `facts` | 2 s | `gather_facts`, paid by every host in every run |
| `validate` / `stage` / `activate` / `commit` / `rollback` | 15 / 20 / 4 / 2 / 4 s | Per-host task time for that tag selection |
| `verify` | 0.5 s | One external serving probe |
| `jitter` | 0.3 | Spread of a host's time; a larger batch waits for a slower straggler |
| `forks` | 20 | `ansible.cfg` |

These numbers are estimates. Each one is a command-line flag
(`--startup 8 --stage 45 ...`), so calibrated figures can replace them.

## Running it

```sh
just bench-rollout                                  # the full matrix below, ~12 min
just bench-rollout --fleet 10,50 --batch 5,10,20    # a quicker subset
just bench-rollout --json results.json              # also write every row
```

Scenarios for every fleet size N and batch size B:

- `rollout`: a normal rollout through all phases. The canary's completion is
  reported separately.
- `fail-stage`, `fail-activate`, `fail-verify`: one edge in the middle of the
  first full batch fails that phase. Activate and verify failures withdraw the
  activation, so the rollback runs are counted.
- `drift-check`: check mode with no canary.
- `drift-yield@25/50/75%`: a deployment asks for the lock that far into the
  check. The figure is how long the deployment waits until the check stands
  aside, or finishes when the request came during its last batch. The CLI's
  `--wait` polls every 2 s on top of that.

Edge names are 12 characters long. The matrix runs again with 60-character names
for B ≥ 10 to show the 512-character host-limit split.

Recovery after a failed rollout means deploying the fixed release again, as a
new rollout: canary first, then the same batches. That costs the same as the
`rollout` row, so it is not repeated.

## Results

Environment: commit `dab89fba`, macOS 15.8 on x86_64 with 16 CPUs, Python
3.15.2, seed 1, default cost model, run with `just bench-rollout`. The full
table is the script's output. These are the rows the conclusions rest on.

### Invocations (measured)

These match `4 × (1 + ⌈(N − 1) / B⌉)` for a rollout and `⌈N / B⌉` for a drift
check, except where the host limit cuts a batch short:

| N | B=1 | B=5 | B=10 | B=20 | B=50 |
| --- | --- | --- | --- | --- | --- |
| 10 | 40 | 12 | 8 | 8 | 8 |
| 50 | 200 | 44 | 24 | 16 | 12 |
| 200 | 800 | 164 | 84 | 44 | 28 |

**Host-limit splitting caps B well below 100.** With 12-character names, a batch
holds at most 39 edges (`39 × 12 + 38 = 506` characters). With 60-character
names it holds 8, whatever B is set to. For example, N=200 with 60-character
names takes 104 runs at B=10, 20 or 50, instead of 84, 44 or 28.

### Duration (simulated)

| N | B=1 | B=5 | B=10 | B=20 | B=50 |
| --- | --- | --- | --- | --- | --- |
| 10 | 732 s | 255 s | 177 s | 177 s | 177 s |
| 50 | 3520 s | 999 s | 583 s | 397 s | 383 s |
| 200 | 14105 s | 3710 s | 2114 s | 1218 s | 1199 s |

The canary takes 77 s at every size: one edge, four runs. A drift check over 200
edges takes 629 s at B=10, 338 s at B=20 and 343 s at B=50.

Above B = `forks` (20), larger batches stop saving time. Only the number of
startups and fact-gathering passes falls, and each run still waits for its
slowest host. At N=200, B=50 is 2% faster than B=20.

### Failure cost (measured outcomes, simulated time)

A failure in the first full batch at N=200:

| B | stage: failed / skipped | activate: failed (withdrawn) | activate: rollbacks, time to stop | verify: succeeded incl. canary / failed |
| --- | --- | --- | --- | --- |
| 1 | 1 / 198 | 1 | 1, 154 s | 1 / 1 |
| 5 | 1 / 198 | 5 | 5, 216 s | 5 / 1 |
| 10 | 1 / 198 | 10 | 10, 270 s | 10 / 1 |
| 20 | 1 / 198 | 20 | 20, 381 s | 20 / 1 |
| 50 | 1 / 198 | 39 | 39, 685 s | 39 / 1 |

A stage failure costs about the same at every B, because nothing has changed
yet. An activation failure withdraws the whole batch, so B is the blast radius.
Every edge in the batch is FAILED, every edge in it serves the previous
configuration again, and withdrawing them takes one rollback run per edge. A
verify failure withdraws only the failing edge; the rest of the batch commits.

### Drift check yielding (simulated)

How long a deployment waits for the lock (12-character names):

| N | B | Lock wait at 25 / 50 / 75 % |
| --- | --- | --- |
| 50 | 5 | 19 / 10 / 20 s |
| 50 | 10 | 27 / 19 / 11 s |
| 50 | 20 | 12 / 18 / 22 s |
| 200 | 10 | 17 / 19 / 26 s |
| 200 | 20 | 6 / 22 / 14 s |
| 200 | 50 | 32 / 5 / 55 s |

The check stands aside only between batches, so the wait is at most one
validate run of a full batch: about 30 s at B ≤ 20 under this model. It grows
with B above `forks`, since a batch then takes more than one wave.

### Controller cost (measured)

Ansible's own process is not included:

| N | B=10 rollout: CPU / wall | Peak Python allocation | Drift check CPU / wall |
| --- | --- | --- | --- |
| 10 | 0.67 s / 1.2 s | 110 KiB | 0.39 s / 0.64 s |
| 50 | 3.5 s / 6.7 s | 471 KiB | 2.1 s / 3.6 s |
| 200 | 12.3 s / 16.3 s | 1.5 MiB | 6.9 s / 9.1 s |

Batch size barely changes these figures. The cost is per edge: each phase
writes its own row, so a rollout makes about seven SQLite commits per edge.
That is roughly 60–100 ms per edge, or 0.1–1% of any rollout's simulated
length. The whole benchmark process peaked at 150 MiB RSS.

## Recommendation

**Keep the default at 10 for now.** The evidence is not strong enough to change
it:

- The only gain from raising B is time, and how much time depends on the
  ratio of `startup + facts` to per-host task time. That ratio is exactly the
  part of the model that has not been calibrated. Going from 10 to 20 cuts a
  200-edge rollout by 42% under this model, but would save less if startup is
  cheaper than assumed.
- The cost of raising B is measured, not modelled. Raising it from 10 to 20
  doubles how many edges an activation failure withdraws (10 → 20) and the
  rollback runs that go with it.
- Values above `forks` (20) buy almost nothing and make the blast radius and
  lock wait worse. With typical name lengths the host limit stops them above
  ~39 anyway. Operators setting B should keep it at or below `forks`.

Revisit this once the disposable-edge figures below exist. If they show
`startup + facts` to be a large share of each run, B=20 (equal to `forks`) is
the candidate default.

## Measuring on disposable edges

Use edges and a controller that serve no traffic: a staging fleet or throwaway
VMs. Production is out of scope.

1. On the controller, put the timing shim first on `PATH` as `ansible-playbook`:

   ```sh
   mkdir -p ~/bench-bin
   ln -sf "$PWD/scripts/ansible-playbook-timing.sh" ~/bench-bin/ansible-playbook
   export BLITZE_BENCH_ANSIBLE_PLAYBOOK="$(command -v ansible-playbook)"
   export PATH="$HOME/bench-bin:$PATH"
   export BLITZE_BENCH_LOG="$PWD/bench-N50-B10.tsv"
   ```

   The shim needs GNU time at `/usr/bin/time` (`apt install time`). Every run
   the controller makes appends its tags, `--limit`, wall time, Ansible CPU and
   max RSS to the log.
2. For each batch size, set `BLITZE_DEPLOYMENT_BATCH_SIZE` and run each
   scenario with a fresh `BLITZE_BENCH_LOG`:
   - normal rollout: `blitzecdn deploy`, with a release that changes something
     on every edge;
   - failure: stop nginx on one edge in the first full batch after its stage
     and before activation. Activation or verification then fails, and the
     batch is withdrawn;
   - drift check: `blitzecdn drift`. Start `blitzecdn deploy --wait 600`
     partway through, and time how long it waits. Use `drift`, not `plan`:
     only the drift check stands aside for a waiter, and `plan` holds the lock
     until it finishes;
   - controller memory: wrap the CLI in `/usr/bin/time -v`.
3. Summarize each log:

   ```sh
   uv run python scripts/rollout_benchmark.py --summarize bench-N50-B10.tsv
   ```

   This prints runs, hosts per run, total, mean and max wall time, Ansible CPU
   and max RSS for each phase.
4. Fit the cost model to the per-phase means: `startup + facts` from the
   one-host canary runs, and the task time from the gradient across batch
   sizes. Then rerun `just bench-rollout` with those flags. Record the fleet,
   edge sizes, controller size, network path and the commands used in this
   page, under a **Measured on real edges** heading kept separate from the
   simulated results.

No real-edge figures have been recorded yet.
