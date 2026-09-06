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

import os

# Typer forces a colour terminal whenever `GITHUB_ACTIONS` is set, whatever it
# is writing to, so under CI its Rich highlighter styles each `--option` and
# splits `--on/--off` into six escape-separated fragments. Tests that assert an
# error names the option it wants then pass locally and fail only on CI. This
# is Typer's own opt-out, and it is read when `typer.rich_utils` is imported —
# hence here, in the file pytest loads before any test module.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

pytest_plugins = ["control_plane_fixtures"]
