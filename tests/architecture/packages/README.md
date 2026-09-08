# Package architecture tests

These tests enforce boundaries between core and optional distributions.

| Module | Boundary |
| --- | --- |
| `test_package_imports.py` | Dependency declarations and public Python imports |
| `test_package_discovery.py` | Plugin registration and installed entry points |
| `test_package_layout.py` | Package layout, documented structure, and shipped Docker paths |
| `test_package_suite_ownership.py` | Test locations and isolation of the core suite |
| `test_package_capability_contracts.py` | Capability contracts and implementation ownership |
| `test_package_ansible.py` | Role ownership, variables, contribution slots, and teardown |
| `test_package_nginx.py` | Firewall vocabulary and generic document rendering |
| `test_package_configuration.py` | Optional settings remain outside core configuration |

`package_boundary_support.py` contains only helpers and constants shared across
these modules. Helpers used by one boundary stay beside its tests.

Run this suite with `just test-one tests/architecture/packages`. The core-only
recipe excludes this directory because these tests inspect installed optional
packages. `../test_lifecycle.py` separately verifies real installation and removal.
