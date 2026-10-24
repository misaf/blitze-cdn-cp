"""Business capabilities of the single BlitzeCDN application.

DNS owns authoring and derivation; serving owns shared policy and effective host
values. Cache, TLS, origins and backup own their operations. Infrastructure
providers live under ``blitzecdn.providers``. Composition registers all shipped
implementations explicitly; installation metadata never selects internal features.

Capabilities keep pure values and contracts separate from services, adapters
and delivery. Cross-capability operations are wired through consumer-owned ports.
"""
