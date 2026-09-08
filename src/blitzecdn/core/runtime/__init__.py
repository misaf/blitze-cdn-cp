"""Core's contact with the machine the control plane runs on.

A subprocess, a file on disk, a log handler, the message broker, and where an
installed distribution's roles and playbooks landed. Nothing here is a policy
or a value; everything here does I/O, which is the whole reason the package
exists — `capabilities/*/domain.py` and every capability contract are refused
these imports as a package rather than one module name at a time.

A package, because naming modules individually is the failure mode: with
`core.filesystem` and `core.process` listed one by one in the layering test's
forbidden tuple, the rule covers exactly the infrastructure modules somebody
remembered to list, and a new one is unguarded until it is not.
"""
