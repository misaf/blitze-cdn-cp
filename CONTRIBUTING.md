# Contributing to BlitzeCDN

This file says where the rules are and which command checks each one. The rules
themselves are in the README, `docs/`, the justfile and the tests. Read them
there, because those copies are the ones the checks enforce.

Report security problems privately, as [SECURITY.md](SECURITY.md) describes,
and not in a public issue.

## Set up

You need git, [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).
uv provides Python, Ansible and ShellCheck. Docker is optional.

```sh
just setup     # install everything, then report what this machine can run
just doctor    # the same report again, with a fix for each missing prerequisite
```

### Stale bytecode after a pull or branch switch

When a module moves or is deleted, its ignored `__pycache__` stays behind, so
the old package directory remains on disk with nothing but bytecode in it.
`just clean-stale` removes untracked bytecode under `src`, `tests` and
`scripts`, then every directory that removal left empty. It keeps tracked
files, directories that were empty before it ran, and everything outside those
three trees, including `.env`, `.venv`, `.state` and worktrees. Add
`--dry-run` to list what would go. Do not use `git clean -fdX` for this, since
it also deletes ignored configuration and operational state.

To run it after every pull, merge and branch switch:

```sh
just hooks-install     # writes post-merge and post-checkout hooks
just hooks-uninstall   # removes them again
```

The hooks go in the clone's shared git directory, so every worktree runs them.
Installation stops without changing anything if a post-merge or post-checkout
hook that it did not write already exists (pre-commit's, for example), or if
`core.hooksPath` is set. In that case, call `scripts/clean-stale.sh --quiet`
from your own hook. A rebase, `git pull --rebase` included, does not run
post-merge, so run `just clean-stale` yourself after one.

[Local development](README.md#local-development) covers running the API and
worker. [Testing without local infrastructure](docs/testing.md) says what needs
Docker and what runs only in GitHub Actions.

## Where a change goes

| Rule | Where it is written | What checks it |
| --- | --- | --- |
| Capability layout: `domain`, `service`, `ports`, `adapters`, `api`, `cli` and the rest | [Code map and boundaries](README.md#code-map-and-boundaries) | `tests/architecture/` |
| Import direction: core never imports a capability, a service never imports an adapter | [.importlinter](.importlinter) | `just lint` (`lint-imports`) |
| One public surface per capability, imports along the declared graph | [Code map and boundaries](README.md#code-map-and-boundaries) | `tests/architecture/test_layering.py` |
| Adding a capability, a per-site setting or a migration | [Where a change belongs](README.md#where-a-change-belongs) | `tests/architecture/`, `tests/platform/test_migrations.py` |
| Transaction boundaries | [docs/transactions.md](docs/transactions.md) | the capability and journey suites |
| Why a larger design choice was made, and adding a new one | [docs/decisions/](docs/decisions/README.md) | review |
| Docstrings on service and port methods | `tests/architecture/test_service_and_port_docstrings.py` | `just test` |
| Strict typing for source, scripts and `tests/support` | `[tool.mypy]` in [pyproject.toml](pyproject.toml) | `just types` |

Shared test helpers go in `tests/support/`. Container harnesses and the
playbooks they run go in `tests/integration/`. Tests import paths from
`support.paths` and do not count `..` from their own file.

## Public surfaces

The HTTP routes and schemas, CLI commands, Ansible variables and database
columns are frozen under `tests/contract/frozen/`. The full OpenAPI document is
committed as [docs/openapi.json](docs/openapi.json). If a change touches any of
these on purpose, run `just refreeze`, read the diff, and commit it with the
change. A change to a surface that has already been released also needs a
version decision.

## Before you push

```sh
just check-quick   # about a minute: lint, types, shell, Ansible, fast tests
just check         # exactly what CI runs; run it before pushing
```

`just check` stops at `docker-lint` when it finds neither hadolint nor a running
Docker daemon. Push the branch, and use `just ci-run` to run CI on it. The
justfile is the authority for every gate, because CI calls its recipes and does
not repeat their commands.

## Branches

| Branch | Holds | Released as |
| --- | --- | --- |
| `7.x` (default) | Fixes, security fixes, docs, dependency updates and non-breaking features | 7.0.1, 7.1.0 |
| `master` | Breaking changes for the next major | 8.0.0 |
| `1.x`-`6.x` | Retired release lines | Nothing, unless revived |

Open a pull request against `7.x` unless the change breaks a public surface;
`gh pr create` and the GitHub UI target it by default. A breaking change goes
to `master`, so pass `--base master`. Dependabot opens its pull requests
against `7.x`.

After a pull request merges into `7.x`, merge `7.x` up into `master`, so every
fix reaches the next major and none has to be ported by hand:

```sh
git switch master && git pull && git merge origin/7.x && git push
```

Opening a new major line, `8.x` from `master`, is one change that adds it to
the push `branches` in `.github/workflows/ci.yml` and
`.github/workflows/edge-image.yml`, moves every `target-branch` in
`.github/dependabot.yml` to it, and updates this section. A line missing from
those lists runs no CI and publishes no edge image when it is pushed. After it
merges, push the branch and make it the default; the previous line is retired
by removing it from the same lists.

## Pull requests

- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md). The
  operational impact section matters most to operators.
- Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for any
  change an operator or API client would notice.
- Keep comments about the current reason for the code. Do not narrate its
  history; git records that.

## Releases

Cut a release from the branch that holds it: `7.x` for 7.y.z, `master` for the
first release of a new major. `just release-version X.Y.Z` moves the version
and every image pin. It turns the Unreleased notes into the release's section
and regenerates `docs/openapi.json`. It refuses to run while Unreleased is
empty. Commit the result, merge it, then tag that commit and push the tag,
which publishes the images:

```sh
just release-tag X.Y.Z && git push origin vX.Y.Z
```

`just release-tag` makes the one kind of tag CI accepts: annotated, signed
with a key GitHub verifies (add yours under Settings → SSH and GPG keys), with
the message `BlitzeCDN X.Y.Z`, on a commit already pushed to a release line.
Every workflow that publishes from a tag runs `scripts/release_tag_check.py`
first and refuses a lightweight or unverified tag, a different message, or a
tag that doesn't match `pyproject.toml`.

A release is a major when its Unreleased notes contain a **BREAKING** entry.
Publish a GitHub release for the tag with its changelog section as the notes.
`just release-notes X.Y.Z` prints them, breaking changes first. Write them to
a file and publish only if that succeeded. Piping straight into `gh` would
still publish, with empty notes, when the version has no changelog section:

```sh
notes=$(mktemp) && just release-notes X.Y.Z > "$notes" && gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-file "$notes"
```

To correct a published release, edit its changelog section rather than the
release, then regenerate its notes the same way, so the two never drift apart:

```sh
notes=$(mktemp) && just release-notes X.Y.Z > "$notes" && gh release edit vX.Y.Z --notes-file "$notes"
```
