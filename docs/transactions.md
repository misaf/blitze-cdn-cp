# Transaction boundaries

Application services decide which database changes succeed or fail together.
Use the injected `UnitOfWork`; do not introduce request-wide or job-wide
transactions. A request or job can include minutes of external work.

```python
# Validate independent input before taking the writer lock.
with self.uow.transaction():
    # Read facts used to decide the write in this same scope, or use the store's
    # expected= compare-and-swap when the original value was read earlier.
    saved = self.edges.replace_edge(updated, expected=current)
    self.events.record(event)
# Both the mutation and its audit event have committed.
```

All participating stores must use the same `Database` instance. Composition
provides this through `Repository`. Creating another repository inside a service
would open a separate transaction and break cross-store atomicity.

## Session and nesting semantics

`Database.transaction()` acquires a per-instance reentrant lock, opens a session,
and immediately issues SQLite `BEGIN IMMEDIATE`. The writer reservation therefore
happens at the service boundary, before reads or writes. A successful outer scope
commits; an exception escaping it rolls back. SQLite admits only one writer, so
keep these scopes short.

Stores call `Database.session()`. Inside a transaction they join its session;
outside one they open and commit/roll back their own scope using deferred
`BEGIN`. This makes an individual store call durable, but does not group separate
calls or guarantee an atomic read/compare/write. Stores require an explicit
transaction for supported `expected=` comparisons.

Nested service transactions join the outer scope. They are **not savepoints**
and do not commit independently. Catch a nested failure only after leaving the
outer transaction; swallowing it inside the outer scope can commit partial work.
The context-local session is scoped to one database and synchronous call chain.
Do not copy it into another thread or asynchronous task.

Related reads need a coherent snapshot too. Release inputs and derived-host
reads group their source tables in a transaction. The current implementation
uses the same writer-reserving boundary for these reads; it does not offer a
separate read-only unit of work. This favors consistency but serializes them
with writers. Reconsider that trade-off if measured contention warrants it.

## Boundaries by operation

| Owner | Atomic database work | Work outside that transaction |
| --- | --- | --- |
| DNS / edges | Desired-state mutation and audit event; derived-host source snapshot | Edge teardown runs before inventory removal |
| Deployments | Admission: deployment, targets, audit, and optional queue entry; finalization: outcome, requirement clearing, and successful rollback adoption | Release preparation precedes admission; artifact publication, Ansible, and serving verification precede finalization |
| Jobs | Queue claim/outcome updates; schedule advancement and enqueueing together | Job handlers execute after the claim commits |
| TLS | Canonical certificate activation, deployment requirement, and audit together | Preflight, CA issuance, material installation, and deployment execution |
| Workflows | Creation/start, each checkpoint, and terminal status in separate short transactions | The operation between checkpoints |
| Releases | Input capture; release retention and pruning; rendered-bundle persistence | Configuration rendering between release retention and bundle persistence |

Deployment release retention and admission are separate commits. A refused
enqueue rolls back deployment admission; it may leave a retained release. That
release is reusable and subject to normal pruning. Do not wrap both phases in
one transaction: release preparation includes expensive rendering.

Workflow checkpoints must commit before proceeding to the next external step.
Do not wrap `workflows.run(...)` in a database transaction: its nested writes
would then remain uncommitted throughout the operation and lose their recovery
value.

## External effects and the local-material exception

For remote operations use short database phases around external execution:

1. Persist admission or an operation checkpoint, then commit.
2. Execute SSH/Ansible, DNS/HTTP probes, or CA issuance without a database transaction.
3. Persist the outcome and related canonical changes together.

Database rollback does not undo a certificate issued by a CA, files installed on
a host, or an activated edge release. Use operation identities, retained
artifacts, workflow evidence, and the operation's existing retry/recovery policy.
Do not blindly rerun an external effect because final database persistence failed.
The deployment file lock coordinates external execution; it is distinct from the
database lock and may remain held across these phases.

Release admission deliberately resolves bounded local certificate material inside
its write transaction before retaining references. Certificate collection holds
the deployment lock and a database transaction while reading retention roots and
deleting unreferenced files. Sharing that database boundary prevents collection
from deleting material between resolution and retention. Filesystem deletion
itself is not rollbackable; collection must remain safe to retry. Do not move
these reads/deletions outside the boundary without replacing that coordination.
Do not add remote calls or expensive rendering to release contributors.

## Verification when changing a boundary

Exercise failure between related writes and verify that none commit. For nested
services, check that stores share one session and that a failed operation leaves
the next transaction usable. At external seams, assert that there is no ambient
database transaction and that the preceding admission/checkpoint is already
durable. Keep the material-retention and collection tests: they protect the
intentional local-filesystem exception.
