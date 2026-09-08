# Edge role contracts

What the roles under `src/blitzecdn/ansible/roles/` guarantee about a converged
edge, asserted against the role files and — where it matters — against a real
`ansible-playbook` run.

| Module | Covers |
| --- | --- |
| `test_role_wiring.py` | Cross-role wiring: the firewall registry, the daemon's configuration |
| `test_edge_runtime.py` | The containerised runtime and the seams around it |
| `test_edge_health.py` | What Docker asks per request and what Ansible asks per deploy |
| `test_runtime_contract.py` | The shared runtime contract the three edge roles read |
| `test_edge_convergence.py` | Converging the shipped contract against a real playbook run |
| `test_edge_permissions.py` | What the converged edge may read and write, and who owns it |
| `test_control_plane_firewall.py` | The controller's own API rules |

`role_contract_support.py` holds the Compose template and the readers shared
across these modules. Helpers used by one module stay beside its tests.
