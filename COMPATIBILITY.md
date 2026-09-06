# Compatibility

What BlitzeCDN promises to the people and programs outside this repository,
and what it costs to change any of it.

Six things bind an outside party: an API client's routes, an operator's
scripts, a third-party wheel's imports and hook signatures, an inventory's
variable names, and the columns of a database. This document says which names
are in each of those, what a change to one obliges, and where the promise is
written down in a form that fails a build rather than a deployment.

## How a release reaches a server

A release is a `vMAJOR.MINOR.PATCH` git tag. An operator installs by cloning
the release branch — `4.x` — and from its first `update` onward that host
follows tags: `install.sh` takes the newest tag in the major line the host is
already on, never crossing a major and never moving backwards. Crossing one is
a separate, deliberate step.

**That makes the major boundary the consent gate**, and it is the reason the
classification below is not bookkeeping. A change released as a minor lands on
every running host at its next update, without anyone choosing it. A change
released as a major waits to be chosen.

Two things are published and they are published differently:

- **The edge runtime image**, built and pushed on every release tag. An edge
  runs it.
- **The Python distributions**, which are *not* on PyPI. Core and the ten
  optional wheels ship inside the same checkout, so the SDK and the plugin ABI
  have no third-party dependant yet — the machinery is complete and nobody
  outside this repository has built against it.

The asymmetry matters when classifying a change. Renaming an Ansible variable
breaks an operator's inventory on the next update; renaming an SDK symbol
currently breaks nobody. The first is a major, and the second stops being free
the day someone publishes a wheel — which is why the goldens hold both.

`4.0.0` is the first release the frozen surfaces exist for. Before it there was
nothing to compare a change against, which is exactly what those files ended.

## The rule

> A name in a frozen surface is a promise. A name outside every frozen surface
> is not, however public it looks.

There is no third category. If something ought to be depended on and no golden
file holds it, that is a defect in this document's enforcement, not a promise
by implication.

## The six surfaces

Each is generated from the running system, not written by hand, and compared
against a committed file in `tests/contract/frozen/`. Each line names the
distribution that promises it, so one golden serves both a full install and a
core-only one.

| Surface | Golden | Who binds to it | Generated from |
| --- | --- | --- | --- |
| **HTTP API** | `http.txt` | API clients | the assembled FastAPI app: every route, and every field of every published schema |
| **Ansible** | `ansible.txt` | operators' inventories and desired-state documents | every role's `meta/argument_specs.yml`, one line per declared key however deeply it nests |
| **Published SDK** | `sdk.txt` | third-party wheels | every public name under `_PUBLIC_SDK_PREFIXES`, at the shallowest path that reaches it |
| **Plugin ABI** | `plugin_abi.txt` | third-party wheels | every hookspec's signature, every contribution dataclass's fields, the supported hook versions, and the bounds on the frameworks the ABI is expressed in |
| **Database schema** | `schema.txt` | every installation, forever | `SQLModel.metadata`: columns, types, nullability, keys, indexes, CHECK constraints, `ondelete`, and the Alembic head |
| **CLI** | `cli.txt` | operator scripts, systemd timers, CI | the assembled Typer tree: every command, option, type and default |

`just refreeze` regenerates all six. It is deliberately not a `--update` flag
on the tests: a flag is one keystroke away from whoever is trying to turn a red
suite green, and the point of these files is that touching one is its own act,
visible in its own diff.

### What each surface records, and what it therefore promises

The generators pin more than names, because a name that survives while its
meaning changes is the failure worth catching:

- **Types and shapes.** An SDK symbol carries its signature, so renaming a
  parameter is a change. An Ansible variable carries its type, choices and
  default, so narrowing `choices` is a change.
- **Behaviour that has no name.** A foreign key carries its `ondelete`, because
  `CASCADE` and `RESTRICT` are opposite answers to the same delete. A column
  carries the decorator class this project defined for it, because
  `UtcDateTime` compiles to `VARCHAR` and reads identically to a plain string
  while refusing naive datetimes and keeping the UTC offset the inventory
  plugin parses.
- **Constraints.** All thirteen CHECK constraints, because `type IN ('A',
  'AAAA')` is a promise to every row an installation will ever write.
- **The frameworks the ABI is written in.** A hookspec returns
  `Sequence[APIRouter]` and a contribution carries a `Typer`, so a wheel
  implementing either is written against FastAPI and Typer as surely as
  against these dataclasses — and pluggy is the mechanism itself. Core pins all
  three and a wheel inherits the bound through `blitzecdn>=4.0.0,<5`, but
  nothing recorded *which* major the contract assumed. They are lines in
  `plugin_abi.txt` now, derived from what the ABI modules import, so a fourth
  library entering a hookspec signature brings its bound with it.

## What is not public

The exclusions carry as much weight as the inclusions, and they are enforced:
`test_an_optional_package_imports_only_public_contracts` fails a wheel that
imports any of them.

- **The composition roots.** `blitzecdn.composition`, `blitzecdn.api.app`,
  `blitzecdn.cli.main`, `blitzecdn.worker`. A plugin that imported one would be
  assembling the thing that is assembling it.
- **Storage.** `blitzecdn.core.persistence` except `schema`, and any
  capability's `adapters`. Stores are reached through ports.
- **Ansible execution and the broker.** `blitzecdn.core.ansible`,
  `blitzecdn.core.runtime.broker`.
- **Anything behind a façade.** A package whose `__init__` re-exports every
  public name of a submodule makes that submodule internal —
  `blitzecdn.core.plugins.types` is not importable by a wheel, because
  `blitzecdn.core.plugins` publishes all of it. This is derived, not listed, by
  `published_surface.facade_private_modules`, so the arrangement of modules
  behind a façade stays free to change. `blitzecdn.core.ports.operations` is
  *not* behind one: its `__init__` re-exports none of it, so those three
  protocols have no shallower path and the module path is the promise.
- **A capability's `service` and `adapters`.** A capability contract another
  capability owns is listed one by one in `_PUBLIC_CAPABILITY_MODULES`; the
  behaviour behind it is not.

Two Ansible variables are shared fleet contracts rather than any one role's:
`blitzecdn_edge_runtime` and `blitzecdn_nginx_sites`, with
`blitzecdn_nginx_http3_listener_owner` beside them. They are named in
`_SHARED_FLEET_VARIABLES` and exempt from the rule that a role's variables
carry its name, because they belong to the fleet.

## Versions

### What a version number means

BlitzeCDN and its optional distributions share a version and release together.

- **Major** — a frozen surface changed in a way that breaks a dependant.
  Removing a route, a command, an option, an SDK name, an Ansible variable or a
  column; narrowing a type, a `choices` list or a CHECK constraint; changing a
  default that an operator relied on; making an optional thing required.
- **Minor** — a frozen surface grew. A new route, command, option, SDK name,
  role or column. Additions move a golden file too, and a golden that only
  gained lines is how a reviewer sees the change was additive.
- **Patch** — no golden file moved.

An optional wheel declares `blitzecdn>=4.0.0,<5`. That upper bound is the first
compatibility lock and the one that acts earliest: `pip` refuses the install
rather than letting a wheel load against a core it was not written for.

**Widening a framework bound is a major version.** Allowing `typer<2` where the
ABI said `typer<1` changes what `CliCommandGroup.app` *is* for every wheel that
constructs one, and no other line of any golden file moves when it happens. The
`framework` lines exist so that change arrives as a diff rather than as a
report from somebody whose plugin stopped loading.

### The hook contract

`HOOK_API_VERSION` is the version of the plugin ABI, declared by every plugin
in its `PluginMetadata` and checked at load. It is a **second** lock, and it
earns its place only on installs that never went through a resolver — an
editable checkout, a vendored tree — where the alternative is an
`AttributeError` inside a hook call that names no plugin.

`SUPPORTED_HOOK_API_VERSIONS` is a set, not a comparison, and that is what
makes a bump survivable. The gate was once `!=`, which meant shipping v2 core
refused every v1 plugin the same day, with no release in which an author could
support both.

Bumping the hook contract therefore has three steps, and they are three
separate decisions in three separate commits:

1. Raise `HOOK_API_VERSION` to 2 and widen `SUPPORTED_HOOK_API_VERSIONS` to
   `{1, 2}`. Both versions load. This is the release that opens the window.
2. Wheels move to `api_version=2` at their own pace.
3. Drop `1` from the set. This is the release that closes the window, and it is
   a major version.

A bump obliges step 1 and step 3 to be different releases. A control plane that
went straight to `{2}` has not deprecated anything; it has broken everything.

## Changing a public surface

When a frozen test fails, the question it asks is **not** "is the new surface
correct". It is "may this change, given who is already depending on the old
one". `just refreeze` exists for when the answer is yes. It is not a way to
make a test go away.

1. **Read the diff.** Every line is something outside this repository can
   depend on.
2. **Decide the kind.** Additive, or breaking? The list under
   [Versions](#versions) is the test, not intuition about how small the change
   feels.
3. **If breaking, deprecate first** — see below. A breaking change that skipped
   a deprecation is a major version with no migration path, which is a thing to
   do on purpose and never by accident.
4. **`just refreeze`, in its own commit**, with the reason in the message. A
   golden regenerated in the same commit as the change that moved it is a
   golden nobody reviewed.

## Deprecation

Each surface deprecates differently, because each is consumed differently. In
every case the old thing keeps working for at least one minor release, and the
removal is the major.

- **HTTP.** The route keeps answering and gains `Deprecation` and `Sunset`
  headers. A response field keeps being sent. A request field keeps being
  accepted and is documented as ignored.
- **CLI.** The command or option keeps working and prints a notice on stderr,
  never stdout — an operator's script parses stdout, and a deprecation notice
  that broke a pipeline would be the outage it was meant to prevent. `--json`
  output never carries the notice.
- **SDK and plugin ABI.** The name keeps resolving. A renamed symbol keeps an
  alias at the old name, and the golden holds both until the removal. A
  contribution field gains a default rather than becoming required; a field
  that must become required waits for the hook-version bump.
- **Ansible.** The variable keeps being read and its replacement takes
  precedence when both are set. A role keeps its old name as a thin wrapper
  that includes the new one, because a role name appears in playbooks this
  project does not own.
- **Database.** See below. There is no deprecation.

A deprecation is not a comment. It is a line in a golden file that is still
there, and its removal is a diff.

## The database is different

Everything else on this list can be deprecated, because a caller can be given
time to stop calling. A schema cannot: the rows already exist.

- **One revision, until release.** `test_the_current_schema_has_no_upgrade_chain`
  asserts there is exactly one. A second revision means something shipped, and
  from that day a column changes by adding a revision, never by editing
  `0001_initial_schema.py`.
- **A schema change is a migration, always.** `Database()` compares the file it
  opens against `Base.metadata` and refuses a mismatch, so a model edited
  without a migration fails at startup rather than half-working.
- **Widening is cheap, narrowing is not.** Adding a CHECK, narrowing one,
  removing a column or making a nullable column `NOT NULL` has to be reconciled
  against rows that already exist, and the migration has to say what happens to
  the ones that do not fit. A migration that would fail on real data is not a
  migration.
- **`ondelete` is behaviour, not decoration.** Changing `RESTRICT` to `CASCADE`
  silently discards data on a delete that used to be refused. It is a major
  version and it needs a note in the release, because nothing an operator does
  will surface it until it has already happened.

## Where this is enforced

Nothing in this document is honour-based. Every claim above fails a build:

| Claim | Enforced by |
| --- | --- |
| The six surfaces are what they were | `tests/contract/test_frozen.py` |
| A wheel imports only public contracts | `test_an_optional_package_imports_only_public_contracts` |
| A façade's submodules are neither imported nor pinned | `facade_private_modules`, and `test_what_the_sdk_publishes_is_what_a_wheel_is_allowed_to_import` |
| A plugin declares the hook contract it targets | `test_a_plugin_states_the_hook_contract_it_was_written_against` |
| A bump can have a deprecation window | `test_the_supported_contract_set_is_how_a_bump_gets_a_deprecation_window` |
| Every site setting is reachable from the CLI | `test_every_patchable_field_can_be_reached_from_the_command_line` |
| A role is named for the wheel that ships it | `test_a_role_is_named_for_the_distribution_that_ships_it` |
| The schema has no upgrade chain yet | `test_the_current_schema_has_no_upgrade_chain` |

`just check` runs all of it, in both configurations — everything installed, and
core alone — because a golden that passed only with every wheel present would
let a whole distribution's surface vanish without a failure.

## Reporting a break

If a release breaks something this document said was stable, that is a bug in
the release, not in the dependant. It is worth reporting with the golden line
you were relying on: the line is the promise, and the diff that removed it is
the evidence.
