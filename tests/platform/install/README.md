# install.sh tests

Behavioural tests for the installer, split by what each group can actually
reach.

| Module | Covers |
| --- | --- |
| `test_install_cli.py` | Lint gates, dispatch, argument parsing, and the validators the script delegates to Python |
| `test_install_standalone.py` | What a `standalone` install guarantees about the host it produces |
| `test_install_teardown.py` | `--uninstall` and `--fresh`, driven for real in the sandbox |
| `test_install_update.py` | `update`, driven as far as its point of no return |
| `test_install_control_plane_role.py` | The `blitzecdn_controlplane` role, which owns the host state |
| `test_install_toolchain.py` | uv, the pinned toolchain the virtualenv is built with |
| `test_install_upgrade.py` | `upgrade`, crossing a major line |

The privileged subcommands refuse to run as a normal user, so the paths that
provision a server cannot be executed directly. What *can* be executed is
everything that decides whether to provision, and the lifecycle paths inside a
sandbox — the script copied with every path redirected under a temp directory,
the root check neutralised, and the privileged commands stubbed on `PATH`.

`install_support.py` holds only helpers shared across these modules: the source
extractors and the sandbox builders. Helpers used by one module stay beside its
tests.
