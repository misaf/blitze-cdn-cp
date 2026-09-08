# Command-line tests

The CLI driven through Typer's runner against a real control plane on a
temporary database, grouped by the command tree each module exercises.

| Module | Covers |
| --- | --- |
| `test_cli_deploy.py` | `plan`, `deploy`, `rollback`, `drift`, and their exit codes |
| `test_cli_edges.py` | Registering, updating and decommissioning an edge |
| `test_cli_zone_policy.py` | TLS, HTTP/3, caching, compression and upload switches |
| `test_cli_zone_origin.py` | How a zone reaches its origin |
| `test_cli_headers.py` | The `BZ-*` visitor-header switches |
| `test_cli_firewall.py` | `zone firewall`: which lists an option replaces |
| `test_cli_records.py` | Proxying a hostname on and off the edge |
| `test_cli_introspection.py` | Help, plugins, versions, `doctor` |
| `test_cli_runtime.py` | Setup, the composition root, refusing to serve unauthenticated |

`cli_support.py` holds the runner and the control-plane builders.

A module declares its own `REQUIRES_CAPABILITIES` for tests that read an
installed capability's own output: the fixture in
`tests/control_plane_fixtures.py` reads that name off the test's module, so an
entry must live beside the test it names.
