# Packaging lifecycle tests

Attaching and detaching a capability through real Python packaging. These build
real wheels, install them into throwaway virtualenvs, and ask the control plane
what it can do — because the property being claimed is about `pip install`, and
a test that mocked the registry would prove only that the mock was written
correctly.

| Module | Covers |
| --- | --- |
| `test_lifecycle_core_alone.py` | The root wheel alone: what it offers and what it must refuse |
| `test_lifecycle_attach_detach.py` | Installing an optional wheel, and uninstalling it again |
| `test_lifecycle_http3.py` | An optional transport over a baseline that is not |
| `test_lifecycle_ansible.py` | The deployment implementation travelling with the wheel |
| `test_lifecycle_core_ansible.py` | Core's own Ansible travelling the same way |
| `test_lifecycle_geoip.py` | One lookup capability behind two unrelated settings |

`conftest.py` owns the three standing environments — core-only, attached,
detached — as session fixtures, so each build and install happens once and every
module shares it. It also applies the `packaging` mark and the no-uv skip to
everything collected here, rather than leaving a `pytestmark` for each module to
repeat: a module added later is marked by sitting in this directory.

`just test-fast` deselects these by marker and `just test-core-only` deselects
this directory by path, so both exclusions survive a new module too.
