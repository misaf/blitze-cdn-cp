# Testing without local infrastructure

A laptop needs Git, [uv](https://docs.astral.sh/uv/) and
[just](https://just.systems/). Docker, a global Ansible and hadolint are
optional. Everything that needs real infrastructure runs in GitHub Actions,
using the same `just` recipes and harness scripts you would run locally.

## Setup

```sh
just setup     # just install, then just doctor
```

`just install` installs Python (through uv), the locked dependencies, Ansible
and ShellCheck into the project environment, and the pinned Ansible collections
into `.state/collections`. You do not need a global Ansible install. The project
environment provides the version CI runs.

The recipes set `ANSIBLE_HOME` to `.state/ansible-home`, so collections in
`~/.ansible` are never used, ansible-lint's included: local lint checks against
the same pinned versions as CI. If the pinned collections are missing,
`just ansible-check` stops before linting and tells you to run `just collections`.

`just doctor` reports every task this machine can run and gives a fix for each
missing prerequisite:

| Task | Needs | Command |
| --- | --- | --- |
| `local` (default) | git, just, uv, a synced environment, the pinned collections | `just doctor` |
| `docker-lint` | `hadolint` on PATH, or a running Docker daemon | `just doctor docker-lint` |
| `integration` | a running Docker daemon on a Linux host | `just doctor integration` |
| `ci` | `gh`, authenticated | `just doctor ci` |

Every task appears in the report, but only the tasks you name decide the exit
status. So on a laptop without Docker, `just doctor` succeeds and
`just doctor integration` fails. If the doctor cannot confirm a prerequisite, it
reports that prerequisite as missing. It never reports it as ok.

## What runs where

| On the laptop | In GitHub Actions |
| --- | --- |
| `just test-one FILE`, `just test-fast` | `just test` with coverage and the packaging lifecycle (quality job) |
| `just lint`, `just types`, `just shell-lint` | The same, in the quality job, on Python 3.15.2 |
| `just ansible-check` (syntax and lint, against project-managed Ansible) | The same, plus Ansible *executing* roles on disposable hosts (install, HTTP/3 jobs) |
| `just check-quick`, the inner-loop gate | `just check`, the full gate, including `docker-lint`, `audit` and `build` |
| `just serve` for local API development | The edge-script contract under Python 3.12, the edge Python floor (quality job; skipped locally when uv has no 3.12) |
| The cache purge against a real nginx, when `nginx` is on PATH | The same, always (quality job; `BLITZE_REQUIRE_NGINX` turns a missing nginx into a failure) |
| Not on a laptop without Docker | `tests/integration/container-install.sh` (install jobs, one per suite) and `tests/integration/http3-edge-integration.sh` (HTTP/3 job) |

`just install` also fetches the reference `age` CLI that `scripts/fetch-age.sh`
pins by version and digest. The backup suite checks encrypted archives against
it and skips those tests when it is missing; CI sets `BLITZE_REQUIRE_AGE_CLI`
so a missing tool fails instead.

The CI workflow calls the recipes and does not repeat their commands, so a gate
added to the justfile is a gate CI runs. `just check` runs exactly the quality
job. Without hadolint or Docker it stops at `docker-lint`. That is deliberate:
a skipped gate must never look like a green one. Use `just check-quick` locally
and let CI run the rest.

## Running CI for a branch

Pull requests and pushes to release branches run CI automatically. To run it on
any branch, including one without a pull request yet, push the branch and run:

```sh
just ci-run           # the current branch; or: just ci-run my-branch
```

This dispatches `.github/workflows/ci.yml` on the commit GitHub has, prints the
run URL and the commit under test, and follows the run until it finishes. It
refuses to start if your local branch and GitHub disagree, because CI would
then test a different commit from the one you have. You can also start the
workflow without `gh`: use **Run workflow** on the repository's Actions tab.

## Reading the results

Each run identifies the commit it tested in two places:

- The **run name** shows the event, the branch and the head commit.
- The **Summary** job writes a table to the run page with the head commit and
  the tested commit. For a pull request, the tested commit is the merge of the
  head into the base. The table also gives each job's result: `success`,
  `failure`, `cancelled` or `skipped`.

The Summary job fails unless every other job succeeded. A skipped or cancelled
job counts as unverified, not as passed. Summary runs last, so it is the check
for branch protection to require.

The run keeps these artifacts for 14 days:

| Artifact | Contents |
| --- | --- |
| `quality-reports` | `coverage.xml` and a JUnit report of the full suite |
| `quality-diagnostics` | Failure only: for each failed real-nginx cache purge test, nginx's configuration and logs, the purge index, and a listing of the cache |
| `install-diagnostics-N` | Failure only, one per install suite: the install host's journal, failed units, listeners, the BlitzeCDN file tree, and the state and logs of every container |
| `http3-integration-diagnostics` | Failure only: the same for the edge host, plus the origin container |

Download them with:

```sh
just ci-download            # the newest CI run on this branch
just ci-download 123456789  # a specific run
```

The artifacts land in `.state/ci/RUN`. A run's diagnostics never include the
host's configuration files, because those files hold the run's API credential.

The harnesses write diagnostics when `DIAGNOSTICS_DIR` is set, so a local run
on a Linux host with Docker can collect the same evidence:

```sh
DIAGNOSTICS_DIR=.state/diagnostics bash tests/integration/http3-edge-integration.sh
```

## What the integration evidence covers

`tests/integration/container-install.sh IMAGE SUITE` runs the `lifecycle` or the
`rollback` stages after a shared install, or both with `all`, the default for a
run by hand. CI runs each suite as its own install job, side by side, to stay
within the job timeout.

The two harnesses run on GitHub-hosted `ubuntu-latest` runners. Each one starts
a privileged Ubuntu 26.04 container with systemd as PID 1, a real Docker Engine
and real apt. That is container-based evidence. It is not proof of behavior on
separate machines over a real network.

| Area | Covered by | How |
| --- | --- | --- |
| Install, re-install, uninstall of a standalone host | install job | Real `install.sh` from the commit under test, cut off from PyPI |
| API access list, credentials, backup and restore | install job | Against the installed control plane |
| Edge convergence through the installed control plane | install job | The local edge, the shipped roles, run through the CLI |
| ACME issue and renewal | install job | Pebble and challenge DNS inside the host |
| Refusing an update outside the release line | install job | `blitzecdn-install update` named at `0.0.1` |
| Recovering from an update that fails health | install job, rollback suite | `update` to a newer-tagged image whose API cannot start; the host is back on the installed image, healthy, with its Compose project and installer, and the state unchanged |
| Rolling back to a backup through a reinstall | install job, rollback suite | Full backup, uninstall, `standalone` again, restore, then `standalone` once more to authorize the restored controller key; configuration, credentials, zones, edges, fleet settings, an uploaded certificate and an ACME account key compared before and after, and SSH to the local edge checked |
| Encrypted backups opened by the independent `age` CLI | install job, rollback suite; quality job | The rollback backup is sealed to an `age-keygen` key and `ssh-ed25519`/`ssh-rsa` keys made off the controller. The pinned `age` CLI opens it with each and the members are checked. On the reinstalled controller, an unrelated key, a tampered and a truncated copy, and no key at all are refused with the state unchanged; the real restore takes the key on standard input, and the key is then searched for on the host's disks, journal and container logs. The backup suite runs the same CLI against every key type, both directions, armored and binary |
| Fresh edge convergence and idempotence | HTTP/3 job | The shipped edge play, from a rendered release |
| HTTP/1.1, HTTP/2, HTTP/3, GeoIP2, Brotli, Under Attack Mode | HTTP/3 job | A real client on the outer Docker network |
| Path-rule redirects answer only after maintenance, the firewall's country, method, path and source rules, the rate limit and Under Attack Mode (challenge, then a cleared redirect) | HTTP/3 job | Zones and rules compiled by the control plane, requested by `tests/integration/redirect_probe.py` from the outer network and by `curl` from the edge host's loopback |
| Configuration reload, invalid configuration rollback | HTTP/3 job | |
| Edge image upgrade, broken-image rollback, engine restart | HTTP/3 job | |
| Runtime teardown and destructive teardown | HTTP/3 job | |
| The whole operator path: zone, release, deploy, verify, recover | `tests/journeys/` | Production objects with Ansible as a double, so this says nothing about an edge |
| Named, prefix and tag purges while nginx fills, replaces and evicts | quality job, `tests/capabilities/cache/test_cache_purge_nginx.py` | A real nginx with the shipped cache key, purged by the shipped scanner; see [concurrent cache activity](design/cache.md#concurrent-cache-activity) |

### Gaps

These are not verified anywhere yet:

- **Control-plane upgrade, happy path.** The install job proves that an update
  outside the release line is refused. Nothing moves a host from the previous
  published release to the commit under test. That can only be tested once a
  release from this line is published: 6.1.0 to the current line is
  intentionally unsupported, because the migration history was reset and the
  current schema refuses a 6.1.0 database.
- **Multi-host API → worker → edge.** In every harness the edge is either the
  control plane's own host (`edge-local`) or a play run on the edge host
  directly. Nothing covers a control plane deploying to a *separate* edge over
  SSH, several edges in one rollout, or an edge that becomes unreachable partway
  through.
- **Real network behavior.** UDP/443 through a real firewall and NAT, public
  DNS, and a public ACME CA are outside a container network.

### Next step for the gaps

Close the upgrade gap with containers once a release from this line is
published: an upgrade suite in `tests/integration/container-install.sh` that installs that
release, updates to the commit under test, and then rolls back the way the
rollback suite does. It runs on the same runners.

The multi-host and real-network gaps need disposable Linux targets. The plan
for that should be scoped as its own issue:

- A workflow, manual or scheduled, creates short-lived VMs from a cloud account
  used for testing only. It deploys the commit under test from a control-plane
  VM to one or more edge VMs and runs the existing probes against them.
- An `always()` step destroys the VMs whether the run passes, fails or is
  cancelled. Every resource gets a label with the run ID, so a sweeper can
  remove anything a crashed runner leaves behind.
- Credentials for that account come from a GitHub environment with required
  reviewers. Production infrastructure is never a target, and nothing in the
  test path can reach it.

No shared staging service is needed for any of this.
