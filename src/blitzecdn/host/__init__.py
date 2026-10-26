"""Scripts that run beside the application rather than as part of it.

None imports the rest of :mod:`blitzecdn`: `install_release` and
`install_inputs` run under the host's own interpreter from the checkout an
update is leaving, and `host_runtime` runs in the control-plane image as root,
beside the application rather than inside it. They live in the package only so
they ship with it.
"""
