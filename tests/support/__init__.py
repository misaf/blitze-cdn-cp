"""What the suites share: fixtures, builders, contract helpers and anchors.

One package, imported as `support.<module>` from anywhere under `tests/`.
A helper used to live beside whichever suite wrote it first and be reached by
putting that directory on `pythonpath`, so where a helper was decided how it
could be imported. Now every shared helper is here, and a module that only one
directory's tests use is still here once a second directory needs it.
"""
