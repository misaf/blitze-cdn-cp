# Architecture

## Original system assessment

The original repository combined a global FastAPI application, loosely typed
service functions, a YAML site database, generated YAML, SQLite deployment
records, a Bash/curl helper, and Ansible roles. HTTP background tasks held an
open `fcntl` lock and launched `ansible-playbook`; process restart lost queued
work. Rollback rewrote current YAML before remote convergence. Validation and
defaults were duplicated in Pydantic, YAML serializers, Ansible assertions,
role defaults, and Jinja. Inventory contained real hosts, root SSH, and a
repository-local private-key path. The firewall role was commented out.

## Target boundaries

```text
CLI / authenticated API
          |
          v
application.ControlPlane       workflow, audit, transitions, rollback
       /             \
      v               v
domain models      infrastructure adapters
invariants         SQLite | atomic files | process lock | Ansible subprocess
                                          |
                                          v
                              declarative Ansible edge roles
```

- `domain` defines immutable CDN site and deployment state plus all values that
  may enter Nginx templates.
- `application` implements use cases and is the sole coordinator of persistence
  and remote execution.
- `infrastructure.database` is the canonical desired-state, deployment-snapshot,
  and audit repository.
- `infrastructure.filesystem` writes an ephemeral, mode-0600 Ansible variable
  file atomically and rejects symlink destinations.
- `infrastructure.ansible` uses an argument array, closed stdin, explicit cwd,
  local temporary directory, timeout, bounded retained output, and a deployment
  lock. It translates OS/process errors at the boundary.
- `api` and `cli` translate presentation and exit/status semantics only.
- Ansible performs remote convergence. It contains defensive validation but no
  product workflow decisions.

## Execution flow

Site mutations are validated, committed transactionally to SQLite, and audited.
They do not touch hosts. Validation renders a snapshot and runs syntax checks.
A deployment acquires the lock, records an immutable snapshot, transitions
`queued → running`, renders that snapshot, and invokes Ansible. It then records
`succeeded`, `failed`, or `timed_out` with bounded output. A controller restart
marks unfinished records `abandoned`.

Rollback selects an explicit successful applied snapshot (or the latest
successful state different from current), deploys it, and only after success
replaces canonical desired state in one database transaction.

## Security and trust boundaries

API keys authenticate operators but are not authorization roles. Secrets are
kept in environment/secret-manager or Ansible Vault and are never accepted as
CLI arguments. SSH host keys are operator-verified. Remote privilege escalation
is explicit per play; global root login is disabled. Domain models constrain
every value inserted into Nginx configuration, including DNS/IP syntax,
durations, ports, names, and absolute certificate paths.

## Extension points

Add a provider by implementing a focused infrastructure adapter and invoking it
from a use case; provider details must not enter domain site validation. Add a
remote responsibility as a namespaced, argument-specified Ansible role. Add a
workflow in `ControlPlane`, then expose it independently through CLI/API.

## Deliberate limits

This release supports one controller node, Debian/Ubuntu edges, Nginx, UFW, and
existing certificates. It does not provision virtual machines, manage DNS,
issue certificates, distribute cache invalidations, or provide active/active
job execution. Those require explicit provider and distributed-state designs.

## Dependency decisions

Typer provides nested CLI commands, validation-friendly annotations, and stable
automation help without maintaining a parser framework. Pydantic is limited to
configuration/domain boundary validation and immutable serialization. Standard
library logging, `sqlite3`, `pathlib`, `tomllib`, and `subprocess` avoid adapters
whose lifecycle or global configuration would obscure safety controls.
`ansible-runner` was not selected because BlitzeCDN needs one local playbook
process and explicit command, timeout, output, and lock semantics; its event and
container features would add state without replacing the distributed job queue
that multi-controller operation actually requires.
