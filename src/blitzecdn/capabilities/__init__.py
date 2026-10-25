"""Business capabilities of the single BlitzeCDN application.

DNS owns authoring and derivation; serving owns shared policy and effective host
values. Cache, TLS, origins and backup own their operations. Hardening and
resolver are edge-host infrastructure with roles and nothing else, and a
strategy of an existing capability — `http/http3`, `security/geoip` — is a
slice nested inside it. Composition registers all shipped implementations
explicitly; installation metadata never selects internal features.

Capabilities keep pure values and contracts separate from services, adapters
and delivery. Cross-capability operations are wired through consumer-owned ports.
"""
