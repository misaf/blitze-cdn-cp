"""The toolkit every capability's routes and commands are written with.

Shared request and response models, the operator dependency, pagination, and
the CLI's output, options and per-invocation container. Capabilities import it;
:mod:`blitzecdn.entrypoints` assembles the application and starts the processes,
and nothing imports that back.
"""
