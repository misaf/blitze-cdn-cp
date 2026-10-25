"""Scripts the host's own interpreter runs, outside the application image.

Neither imports :mod:`blitzecdn`: `install_release` runs from the checkout an
update is leaving, and `host_runtime` is copied into /etc/blitzecdn and run by
the bootstrap interpreter. They live in the package only so they ship with it.
"""
