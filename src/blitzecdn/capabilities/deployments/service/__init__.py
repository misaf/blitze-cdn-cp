"""What this capability decides, apart from the values it decides about.

Four modules, one layer. `convergence.py` turns a stored snapshot into a run of
Ansible and records what happened; `rollback.py` owns what rolling back
*means*; `reporting.py` owns how a recorded run may be read as evidence;
`validation.py` answers whether desired state is coherent without publishing
anything. The last three were already separate files for their own good
reasons — each protects an invariant that is easy to lose inside a service
that is mostly about the run — but they were separate files *beside*
`service.py`, which made the layering rule a list of four names.

Deliberately not a re-exporting package: the capability's public face is
`blitzecdn.capabilities.deployments`, and a caller that wants the service asks
for it there. This `__init__` exists to name the layer, not to widen it.
"""
