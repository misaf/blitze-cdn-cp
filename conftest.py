"""The workspace's root pytest configuration.

The one job here is making the control plane's shared fixtures reachable from
every distribution's tests. `tests/control_plane_fixtures.py` holds them —
`settings`, the fake runner, the fake stores — and this registers it as a
plugin for the whole run, which is what lets
`packages/blitzecdn-cache/tests/` use `settings` without either copying the
fixture or reaching up into a directory it does not own.

It is a plugin rather than a `tests/conftest.py` because a `conftest.py` is
scoped to its own directory tree, and the workspace's tests deliberately live
in several: the control plane's under `tests/`, and each optional capability's
inside the package that ships it.
"""

pytest_plugins = ["control_plane_fixtures"]
