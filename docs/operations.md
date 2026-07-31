# Operations

## Deployment checklist

1. Verify backups of `.state/control-plane.db`.
2. Verify inventory addresses and SSH host fingerprints out of band.
3. Confirm the non-root deployment user has narrowly managed sudo access.
4. Set management CIDRs; an empty list deliberately fails.
5. Run `blitzecdn doctor`, `validate`, and `plan`.
6. Review check-mode output and apply with `deploy --yes`.
7. Inspect `status DEPLOYMENT_ID` and edge health externally.

## Troubleshooting

- Authentication 503: configure `BLITZE_API_KEYS`; malformed/short keys fail at
  startup.
- Inventory missing: create `ansible/inventory/hosts.yml` or set
  `BLITZE_INVENTORY` to an absolute path.
- SSH host-key failure: validate the new fingerprint; never bypass checking.
- Firewall assertion: provide at least one trusted CIDR containing the current
  management source before retrying.
- Nginx validation failure: inspect bounded stderr/status, correct desired state,
  plan, and deploy. Nginx is not reloaded after a failed validation.
- Deployment conflict: inspect current controller process and lock owner. Do not
  delete a live lock file; filesystem locks release when their process exits.
- Timeout: confirm network/package-repository health and host state. A timeout is
  not proof that no remote changes occurred; rerun plan before retrying.
- Disk exhaustion: stop new work, preserve the database, free space outside
  `.state`, run SQLite integrity checks, then validate before deployment.

## Release

Run every quality gate, build wheel/sdist, inspect their contents, update the
version and changelog, tag the reviewed commit, and publish from an isolated CI
identity. Releases must never contain `.env`, `.state`, inventories, SSH keys,
Vault passwords, or certificate keys.
