"""Where the control plane is put together, and the only place that decides.

Three modules, one job: choosing which concrete thing satisfies a port, and
which parts make one running control plane.

* :mod:`~blitzecdn.composition.control_plane` — the composition root proper.
  It builds the adapters, injects them into the capability services, loads the
  plugins and hands each one the finished platform.
* :mod:`~blitzecdn.composition.repository` — which capability stores sit on one
  SQLite database. A bundle, not a layer: each store already satisfies its
  capability's port, so no service is handed more of persistence than it asked
  for.
* :mod:`~blitzecdn.composition.scheduler` — which of the contributed jobs get
  triggers, and how often. It runs inside the API process; it is not one.

All three were loose modules at the package root, beside `worker.py` and
`install_handoff.py`, which meant the root answered two unrelated questions at
once and answered neither: "how is a control plane assembled" and "which
processes exist". The root is the processes now — `api/`, `cli/`, `worker.py`,
`install_handoff.py` — and `test_a_loose_module_at_the_root_starts_a_process`
holds that.

`repository` was `blitzecdn.persistence`, one import away from
`blitzecdn.core.persistence` and forever explaining in its own docstring which
of the two it was. What core keeps is what a capability builds *on* — the
engine, the write lock, the Unit of Work, the schema. What is chosen here is
which stores go on one file.

The names re-exported below are the whole public face. An entry layer, a
capability's seam and an installed distribution all import
`blitzecdn.composition`; nothing outside this package imports one of its
modules by name, so where a piece of the wiring lives stays this package's
business.
"""

from blitzecdn.composition.control_plane import (
    BUILTIN_PLUGINS,
    ControlPlane,
    FleetRunner,
    build_control_plane,
    load_control_plane_plugins,
)
from blitzecdn.composition.repository import Repository
from blitzecdn.composition.scheduler import build_scheduler

__all__ = [
    "BUILTIN_PLUGINS",
    "ControlPlane",
    "FleetRunner",
    "Repository",
    "build_control_plane",
    "build_scheduler",
    "load_control_plane_plugins",
]
