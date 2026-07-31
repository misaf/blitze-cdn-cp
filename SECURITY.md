# Security policy

Report vulnerabilities privately to the repository maintainers; do not open a
public issue containing exploit details or credentials. Rotate any exposed key
immediately and review audit/deployment history.

BlitzeCDN assumes a trusted controller, verified SSH host keys, protected state
directory, secure secret injection, and least-privilege deployment accounts.
The API key mechanism provides authentication and attribution, not fine-grained
authorization. Bind the API to localhost unless a TLS-authenticated reverse
proxy and network policy protect it.
