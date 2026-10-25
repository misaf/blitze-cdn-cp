"""The processes the control plane runs: the API, the CLI, the worker, and the
installer's post-bootstrap handoff. Each builds its services from
:mod:`blitzecdn.composition` and calls one; nothing imports them back."""
