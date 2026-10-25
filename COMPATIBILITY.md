# Compatibility policy

BlitzeCDN is currently unreleased and has no deployed compatibility obligations.
The application ships as one `blitzecdn` distribution. Its capability modules and
runtime providers are internal implementation boundaries, not independently
versioned products. There are no compatibility packages for retired
`blitzecdn-*` distributions or their import namespaces.

## Published surfaces

`tests/contract/frozen/` records the interfaces that installations and clients
can bind to. Intentional changes are regenerated with `just refreeze`, reviewed
as diffs, and committed with the implementation.

| Surface | Snapshot | Authority |
| --- | --- | --- |
| Command line | `cli.txt` | assembled command tree |
| HTTP API | `http.txt` | routes and request/response schemas |
| Ansible | `ansible.txt` | role argument specifications |
| Database | `schema.txt` | schema and Alembic revision |

All shipped surface rows belong to `blitzecdn`. There is no third-party
extension mechanism, and therefore no published extension ABI or SDK: the
conditions for adding one, and the order it would arrive in, are in
[ADR 0021](docs/decisions/0021-capability-roster-and-process-composition.md).

The generators are exercised by `test_the_command_line_is_what_it_was`,
`test_the_http_api_is_what_it_was`, `test_the_ansible_interface_is_what_it_was`
and the other frozen-contract checks. `test_every_surface_found_something_to_freeze`
prevents a broken generator from approving an empty snapshot.

## Internal evolution and persistence

Cross-capability business calls use public domain values and ports. Services,
adapters, and composition details are private. Runtime providers own concrete
Ansible and Nginx resources; installing the application always includes them.
Configuration and declared edge capabilities determine what can be realized.

Until release, schema changes edit the one initial migration. Retired release
input versions are refused explicitly, without upcasters or dual representations.
After release, persisted data changes require a migration, and changes to a
published surface require an explicit version decision.

Run `just check-quick` while developing and `just check` before pushing. The
installed-application contract builds and installs the single wheel in a fresh
virtual environment outside the checkout, checks all provider resources, and
proves command discovery needs no database.
