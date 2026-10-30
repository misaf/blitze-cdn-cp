"""Scripts that run beside the application rather than as part of it.

None imports the rest of :mod:`blitzecdn`. `install.sh` runs `install_inputs`
and `image_releases` in a control-plane image before anything on the host
changes, and `host_runtime` runs in that image as root, beside the application
rather than inside it. They live in the package only so they ship with it.
"""
