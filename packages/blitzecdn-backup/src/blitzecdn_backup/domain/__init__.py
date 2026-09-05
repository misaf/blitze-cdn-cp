"""What a backup archive is, and what may be inside one.

`manifest.py` is the whole of it: the components an archive can carry, the
manifest that records them, and the member rules a restore refuses an archive
for. One subject, so one module — the directory says which layer it is.
"""

from blitzecdn_backup.domain.manifest import (
    BACKUP_FORMAT_VERSION,
    MANIFEST_NAME,
    BackupComponent,
    BackupManifest,
    backup_filename,
    member_component,
    parse_manifest,
    unsafe_member,
)

__all__ = [
    "BACKUP_FORMAT_VERSION",
    "MANIFEST_NAME",
    "BackupComponent",
    "BackupManifest",
    "backup_filename",
    "member_component",
    "parse_manifest",
    "unsafe_member",
]
