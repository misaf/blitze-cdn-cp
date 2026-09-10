# Releases: what the fleet is asked to serve, as an addressable value

Status: accepted; documents the design in `capabilities.releases`, and the
change it forced on `capabilities.deployments`.

## Context

"What should the fleet be serving" was answered in three places, at three
different times, and no two of them could be compared afterwards.

**When a deployment was queued**, `Repository.snapshot()` serialised the zones,
their rules and their records into a JSON string and stored a full copy of it on
the deployment row. The drift timer fires hourly, so the table grew by a
complete desired state every hour whether or not anything had changed, and
nothing said that two of those documents were the same document.

**When a run reached the runner**, `DesiredStateRenderer` decoded that string,
derived the virtual hosts, asked every installed plugin for its variables, and
wrote the result to `generated_vars_path` moments before Ansible read it. That
document was never recorded. After the fact — the thing an operator actually
wants after an incident — "what did that deployment send" had no answer, only a
re-derivation from state that had since moved on.

**Somewhere between the two**, `DeploymentValidation` asked the plugin registry
what it objected to about each site, against whatever happened to be installed
at that moment.

Three consequences followed from that arrangement, and each of them was a real
defect rather than an aesthetic one.

*Compilation and I/O were fused.* Deriving a host needed no database and no
network, but the only way to run it was to have both, so the interesting
questions — is this reproducible, does this state compile to the same document
twice — were not askable.

*A capability was validated against the controller and never against an edge.*
`PluginRegistry.validate_site` answers "is a wheel installed here". Whether the
*edge* runtime can execute what the wheel rendered is a different question, and
the only thing asking it was the `blitzecdn_nginx` role's build probe, halfway
through a converge that was already touching hosts.

*The rule that decided a setting was reported; the setting's source was not.*
`GET /v1/domains/{domain}/resolve` names the winning rule, which settles the
match. It does not settle where any individual value came from — and a rule that
wins while overriding nothing relevant leaves the zone's value in place, so an
operator told only "rule `api` applied" goes and edits the rule.

## Decision

**A release is a value, and its identity is the digest of its own contents.**
`compile_release` is a pure function: canonical state in, plus the capabilities
installed and the edges the run is aimed at, and a `Release` out. It touches no
database, opens no socket, writes no file and reads no clock. Compiling twice
over equal inputs produces an equal digest, which is what makes "has anything
changed since the last deploy" a string comparison rather than a fleet-wide
check-mode run.

`Release` therefore carries no timestamp, no operator, and no status. All three
are facts about a *deployment of* a release, and a release is converged more
than once — a rollback re-converges an old one, a retry re-converges the one
that failed. Putting any of them on it would make two identical configurations
two different releases, which is exactly the comparison it exists to make cheap.

**The compiler version is part of the identity.** A change to the compiler that
would produce a different artifact from identical inputs is a change to what the
edges are served, and without `COMPILER_VERSION` in the digest yesterday's
release would go on looking current while the fleet served an artifact no code
in the tree still produces.

**Findings, not exceptions.** A release that cannot be converged is still
compiled and carries the reasons. `blitzecdn validate` and `blitzecdn deploy`
ask the same question, and a compiler that raised on the first problem would
hand an operator one hostname per run.

**An unservable release carries no artifact.** Rendering happens only when there
is nothing to report, and this is not an optimisation. A capability contributes
its variables by being *asked*; a capability the site needs but the installation
does not have cannot be asked at all. Rendering ahead of the check turned "this
site requests a capability you have not installed" into whatever exception the
nearest contributor happened to raise — for `blitzecdn-certificates`, a
`NotFoundError` about material that was never issued because the site had
already been refused. `Release.artifact` raises `UnservableReleaseError` with the
findings instead.

**Capabilities are validated against the selected edges as well as against the
controller.** `Edge.capabilities` is what an operator declares an edge's runtime
provides. `None` — the default, and what every edge registered before the field
existed reads as — means "whatever the controller has installed", which is
precisely the assumption the control plane made when it had no way to ask. So
the check is opt-in and adding it changed no existing fleet's outcome. It is a
*declaration*, not an observation: the role's build probe remains the authority
on what is actually on the host, and this is the same question asked early
enough that the answer costs nothing.

**A release explains itself.** Every compiled host carries each effective
setting attributed to `RULE`, `ZONE` or `DEFAULT`. The distinction between the
last two is drawn by comparing against the field's declared default rather than
by remembering which fields a patch named, because an operator who explicitly
sets a value to what it already was has changed nothing an edge can observe.

**Inputs are stored beside the release, and a rollback adopts them.** Converging
an old artifact alone would put the edges back and leave the control plane still
asserting the newer state — so the next ordinary reconciliation would compile
the state the control plane still holds and undo the rollback, quietly, within
the hour. `adopt_inputs` restores the zones, rules and records, which is what
makes a rollback a change of desired target rather than a temporary override.

**Deployments hold a reference, not a document.** `deployments.release_id` is a
foreign key to a content-addressed row, so two deployments of unchanged state
name one release. Release pruning asks the deployments store — through the
`ReleaseReferences` port, never by reading its table — which releases something
still names, because a release a deployment converged is the fleet's way back to
that state and age is no reason to remove it.

## Alternatives rejected

*Keep the snapshot and add a digest beside it.* The digest already existed —
`snapshot_digest` — and it addressed the *inputs*, not the artifact. Two
controllers with different wheels installed compiled the same digest to
different documents, so the one comparison an operator wants to make was the one
it could not support.

*Record the rendered document on the deployment row.* It answers "what did we
send" and nothing else: still one copy per run, still no way to tell two equal
configurations apart from two different ones, and still no separation between
the part that is a function of state and the part that is I/O.

*Store the derived virtual hosts in the inputs.* Rejected for the reason
[0001](0001-zone-policy-and-composition.md) gives and
`ReleaseInputs` restates: a rollback restoring both would restore a document
free to disagree with itself.

## Consequences

`blitzecdn validate` is cheaper and says more: it compiles, reports every
finding at once, and only then asks Ansible to parse the play. It records
nothing, so an operator can run it as often as it takes.

`blitzecdn release show` answers what a deploy is about to send without sending
it, and `blitzecdn release explain` answers where a setting came from. Both work
against a recorded release or against current state.

The deployments table stopped carrying desired state. `DeploymentStore` no
longer needs a `snapshot_source` and no longer reaches across three other
capabilities' tables to build one.

A canary is now validated against the edges it will actually reach. `--limit`
narrows the compilation's targets through the same matcher
(`matches_edge_limit`) that expands the limit into the host list Ansible is
given, so the two cannot disagree about what a canary is.

An installation whose edges declare no capabilities behaves exactly as it did.

## Code and verification

- [The release compiler](../../src/blitzecdn/capabilities/releases/service/compiler.py)
- [The release value and its digests](../../src/blitzecdn/capabilities/releases/domain/release.py)
- [Canonical inputs](../../src/blitzecdn/capabilities/releases/domain/inputs.py)
- [Setting provenance](../../src/blitzecdn/capabilities/releases/domain/explanation.py)
- [The release service](../../src/blitzecdn/capabilities/releases/service/releases.py)
- [Release persistence](../../src/blitzecdn/capabilities/releases/adapters/persistence.py)
- [Convergence](../../src/blitzecdn/capabilities/deployments/service/convergence.py)
- [Rollback policy](../../src/blitzecdn/capabilities/deployments/service/rollback.py)
- [Compiler tests](../../tests/capabilities/releases/test_compiler.py)
- [Release service tests](../../tests/capabilities/releases/test_release_service.py)
- [Capability graph](../../tests/architecture/test_layering.py)
