# Contributing

Use Python 3.14 and install `.[dev]`. Keep domain rules independent of FastAPI,
Typer, SQLite, subprocess, and Ansible. Application code may depend on domain
and injected adapters; infrastructure must translate low-level failures.

Add behavioral tests for every change and error path. Run the commands in the
README before submitting. Do not suppress lint, typing, security, or test
findings without a documented technical reason. Never add real hosts or secrets
to fixtures. Breaking changes are allowed before 1.0 but must be documented.
