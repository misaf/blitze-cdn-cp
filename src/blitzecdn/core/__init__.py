"""What every capability stands on, arranged by who may import each part.

The only layer-first package: :mod:`~blitzecdn.core.domain` holds values with
no I/O; :mod:`~blitzecdn.core.ports` the protocols services depend on;
:mod:`~blitzecdn.core.persistence` the SQLite engine, schema and stores;
:mod:`~blitzecdn.core.runtime` subprocesses, files and logging;
:mod:`~blitzecdn.core.ansible`, :mod:`~blitzecdn.core.config` and
:mod:`~blitzecdn.core.features` Ansible execution, configuration and the
feature mechanism; and :mod:`~blitzecdn.core.exceptions` the errors every layer
may name. The import contracts name these packages, so where a file goes
decides who may depend on it.
"""
