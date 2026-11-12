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

## Pull requests

- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md). The
  operational impact section matters most to operators.
- Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for any
  change an operator or API client would notice.
- Keep comments about the current reason for the code. Do not narrate its
  history; git records that.

## Releases

`just release-version X.Y.Z` moves the version and every image pin. It turns
the Unreleased notes into the release's section and regenerates
`docs/openapi.json`. It refuses to run while Unreleased is empty. Commit the
result, then push the `vX.Y.Z` tag, which publishes the images.
