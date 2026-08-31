"""Plugins that stand in for separately installed distributions.

These are what a `blitzecdn-waf` package would ship: a module of `@hookimpl`
functions and an entry point pointing at it. Nothing here is imported by the
control plane — the tests load them through the real entry-point machinery, so
what is exercised is the path an operator's `pip install` would take.
"""
