# blitzecdn-backup

Disaster recovery for the BlitzeCDN control plane, as an optional distribution.

`blitzecdn backup create` takes everything a rebuilt controller would otherwise
have lost — the database, the certificates and their keys, the ACME account and
the configuration — and `blitzecdn backup restore` puts back exactly what an
archive's manifest lists.

Attach it:

```bash
uv add --package blitzecdn blitzecdn-backup   # in this workspace
pip install blitzecdn-backup                  # beside an installed control plane
```

Detach it:

```bash
uv remove --package blitzecdn blitzecdn-backup
pip uninstall blitzecdn-backup
```

Detaching is non-destructive. Archives already written stay where they are, and
nothing in the control plane's database belongs to this package, so
re-attaching restores the capability with no migration and no data loss. What
disappears is the `blitzecdn backup` command group; `install.sh` takes a
database backup before an update, so a controller that is updated in place
should keep this package installed.

See [PLUGINS.md](../../PLUGINS.md) for the optional-capability contract.
