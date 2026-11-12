"""Shared hooks for the cache capability's tests."""

from __future__ import annotations

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Keep each phase's report on the item, so a fixture can tell it failed.

    The real-nginx purge tests keep nginx's logs, the index and a listing of
    the cache only when the test using them failed.
    """
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"report_{report.when}", report)
