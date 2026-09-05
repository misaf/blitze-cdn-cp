"""What every capability stands on, organised by who is allowed to import it.

`core` is a layer rather than a slice, so unlike `capabilities/` it is arranged
layer-first — and it is the only package in this workspace that is:

* :mod:`~blitzecdn.core.domain` — values and vocabulary, no I/O anywhere;
* :mod:`~blitzecdn.core.ports` — the protocols a service declares instead of
  naming an implementation;
* :mod:`~blitzecdn.core.persistence` — the SQLite engine, the physical schema
  and the stores on it, private but for `persistence.schema`;
* :mod:`~blitzecdn.core.runtime` — subprocesses, files, logging, the broker,
  and where an installed distribution landed;
* :mod:`~blitzecdn.core.ansible`, :mod:`~blitzecdn.core.config`,
  :mod:`~blitzecdn.core.plugins` — already packages, unchanged;
* :mod:`~blitzecdn.core.exceptions` — the one module every layer may name,
  which is why it is still a file here.

These were nineteen modules in one directory. Nothing was wrong with any of
them individually; what was wrong is that the architecture tests had to
enumerate the infrastructure by name — `core.database`, `core.database_engine`,
`core.database_models`, `core.filesystem`, `core.process`, `core.broker` — to
say "a capability's domain may not import these". A rule written as a list of
module names covers the modules somebody remembered, and a new one is
unguarded until a reviewer notices. The same rules now read
`blitzecdn.core.persistence` and `blitzecdn.core.runtime`, so a module lands
inside the rule the moment it is created, and the directory a file goes in is
the same decision as who may depend on it.
"""
