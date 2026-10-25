"""What every capability stands on, organised by who is allowed to import it.

`core` is a layer rather than a slice, so unlike `capabilities/` it is arranged
layer-first — and it is the only package in this workspace that is:

* :mod:`~blitzecdn.core.domain` — values and vocabulary, no I/O anywhere;
* :mod:`~blitzecdn.core.ports` — the protocols a service declares instead of
  naming an implementation;
* :mod:`~blitzecdn.core.persistence` — the SQLite engine, the physical schema
  and the stores on it, private but for `persistence.schema`;
* :mod:`~blitzecdn.core.runtime` — subprocesses, files, logging,
  and where the installed distribution landed;
* :mod:`~blitzecdn.core.ansible`, :mod:`~blitzecdn.core.config`,
  :mod:`~blitzecdn.core.features` — configuration, Ansible execution, and
  the feature mechanism;
* :mod:`~blitzecdn.core.exceptions` — the one module every layer may name,
  which is why it is still a file here.

The import rules name `blitzecdn.core.persistence` and
`blitzecdn.core.runtime` as packages, so a module lands inside a rule the moment
it is created, and the directory a file goes in is the same decision as who may
depend on it.
"""
