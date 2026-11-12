"""The test suite's root pytest configuration.

It prepares the process before any test module is imported:

- it switches off Typer's forced colour terminal, so CLI error messages are
  plain text on CI as well as locally;
- it registers `support.control_plane` — the shared `settings`, fake runner and
  fake store fixtures — and `support.composition` — the `policy_composition`
  fixture that recomposes the site policy for one test — as plugins for the
  whole run;
- it imports the composition root, which composes the site policy, because a
  test module may build a site at import time.
"""

from __future__ import annotations

import os

# Typer forces a colour terminal whenever `GITHUB_ACTIONS` is set, whatever it
# is writing to, so under CI its Rich highlighter styles each `--option` and
# splits `--on/--off` into six escape-separated fragments. Tests that assert an
# error names the option it wants then pass locally and fail only on CI. This
# is Typer's own opt-out, and it is read when `typer.rich_utils` is imported —
# hence here, before anything below can import Typer.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"


import blitzecdn.composition  # noqa: F401  # composes the site policy on import

pytest_plugins = ["support.control_plane", "support.composition"]
