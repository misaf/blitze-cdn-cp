# How a failure reaches a caller

Status: accepted; documents the design in `core.exceptions`, and the two
projections in `cli.main` and `api.app`.

## Context

Every operation can fail, and both delivery surfaces have to turn the failure
into a number an unattended caller branches on: an exit code for a shell, a
status code for a client. [0004](0004-what-each-delivery-surface-carries.md)
records which *operations* each surface carries. This record is about what each
surface says when one of them fails.

The two surfaces had each grown their own answer. `cli.main` walked an ordered
tuple of exception types to an `ExitCode`; `api.app` registered six
near-identical FastAPI handlers, one per type. Both lists named the same
exceptions and neither knew the other existed, and the CLI's comment said it
"mirrored" the API — a claim nothing checked.

They had already drifted, at the case least likely to be noticed and most
likely to matter. A `BlitzeError` that neither list named exited **2**,
`INVALID_INPUT`, on the command line and answered **503** over HTTP. The first
tells an operator they typed something wrong; the second tells a client the
service is briefly down and to retry. `PluginError` — the one concrete subclass
neither list named — is neither. A plugin that will not load is an installation
that is broken: retrying cannot help, and the operator's command line is not
where the fix is.

## Decision

Core owns the taxonomy. Each delivery layer owns only its projection of it.

`FailureKind` names what happened — `BUSY`, `CONFLICT`, `NOT_FOUND`,
`EXECUTION`, `CONFIGURATION`, `INTERNAL` — and `classify` maps an exception to
one, walking `_CLASSIFICATION` most-specific-first because `DeploymentBusyError`
is a `ConflictError` and would otherwise be matched by its parent. Neither
delivery layer classifies an exception any more; each holds one dict from kind
to its own number.

The distinctions stay separate for the reason they always did: a conflict is
not a bad request, and a dependency that misbehaved is not a controller that is
down. Collapsed onto one code, a systemd timer cannot tell "a deployment is
already running, come back shortly" from "you typed the site name wrong".

`INTERNAL` is the resolution of the drift. It covers `PluginError` and any
unmapped `BlitzeError`, and it is deliberately *not* the operator's input: we
do not know that it was. It answers exit 3 (`CONFIGURATION` — the fix is the
installation, and no retry helps) and HTTP 500 rather than 503, because a
broken installation is not a service that is briefly unavailable and inviting a
retry that cannot succeed is worse than admitting it.

### Why not one table with both numbers

The obvious arrangement — a single tuple in core holding the exception, the
exit code and the status code — was rejected. It would put HTTP's vocabulary
in `core`, which the architecture tests forbid for good reason: core has no
business knowing that a delivery layer speaks HTTP at all, and a second HTTP
surface, or a first non-HTTP one, would have to edit core to be added. Naming
the taxonomy in core and the vocabulary in each layer keeps the layering and
still leaves exactly one place where "what kind of failure is this" is decided.

### Why the API is now one handler

Starlette resolves a handler by walking the raised exception's MRO, so one
handler registered on `BlitzeError` catches every subclass. Which status each
kind gets is a dict rather than the registration order of six decorators — the
`Retry-After` header on `BUSY` is the one per-kind detail left, and it stays in
the API because it is HTTP's, not core's.

## Consequences

`ValidationError` and `OSError` stay outside the taxonomy on both surfaces, and
deliberately: neither is a `BlitzeError`. The API answers `ValidationError` with
422 and the CLI folds both into `INVALID_INPUT`, which is the one place that
code is still correct — the value handed in, or the environment it named, really
was wrong.

Both projections are subscripted rather than `.get()`, so a kind added to core
without a line in a layer is a `KeyError` at the moment a failure is being
reported. That is the worst possible time to find out, so the parity test is
what actually holds this: it fails at build time instead.

The CLI cannot distinguish `CONFIGURATION` from `INTERNAL`, since both exit 3.
That is accepted rather than overlooked — exit codes are a coarser vocabulary
than status codes, both mean "fix your installation, do not retry", and adding
a code to a published surface to split them buys an operator nothing they
cannot read in the message.

## Code and verification

- [Taxonomy and classification](../../src/blitzecdn/core/exceptions.py)
- [Exit code projection](../../src/blitzecdn/cli/main.py)
- [HTTP status projection](../../src/blitzecdn/api/app.py)
- [Parity and classification tests](../../tests/contract/test_delivery_parity.py)
- [Error mapping over HTTP](../../tests/api/test_api.py)
