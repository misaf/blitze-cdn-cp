"""Core's contact with the machine the control plane runs on.

A subprocess, a file on disk, a log handler, the message broker, and where an
installed distribution's roles and playbooks landed. Nothing here is a policy
or a value; everything here does I/O, which is the whole reason the package
exists — `capabilities/*/domain.py` and every capability contract are refused
these imports as a package rather than one module name at a time.

That was the failure mode of the flat layout it replaces. `core.filesystem`
and `core.process` were named individually in the layering test's forbidden
tuple, so the rule covered exactly the infrastructure modules somebody had
remembered to list, and a new one was unguarded until it wasn't.
"""
