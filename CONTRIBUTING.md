# Contributing

Use Python 3.14 and install `.[dev]`. Keep domain rules independent of FastAPI,
Typer, SQLite, subprocess, and Ansible. Application code may depend on domain
and injected adapters; infrastructure must translate low-level failures.

Roles are consumed from the collection pinned in `ansible/requirements.yml`,
never from a local path. Editing a checkout of `blitze-cdn-edge` therefore has no
effect on a control-plane test run until the collection is rebuilt and
reinstalled:

```bash
BLITZECDN_DEV=1 BLITZECDN_EDGE_PATH=../blitze-cdn-edge ./install.sh
.venv/bin/pytest tests/test_contract.py --no-cov -q
```

The contract tests read the installed collection, so they silently skip when it
is absent. Install it before trusting a green run.

Add behavioral tests for every change and error path. Run the commands in the
README before submitting. Do not suppress lint, typing, security, or test
findings without a documented technical reason. Never add real hosts or secrets
to fixtures. Breaking changes are allowed before 1.0 but must be documented.
