# Controller configuration

Platform settings live in `src/blitzecdn/core/config/settings.py`. Capability
settings live beside their owner:

| Group | Definition |
| --- | --- |
| Certificates and Automatic SSL | `capabilities/tls/settings.py` |
| Under Attack Mode | `capabilities/security/settings.py` |
| MaxMind credentials | `capabilities/security/geoip/settings.py` |
| Backup location and retention | `capabilities/backup/settings.py` |

These are application settings. Database-managed domain policy, cache policy,
and other desired CDN state remain separate.

## Sources and compatibility

`Settings.from_environment()` loads sources in this order, highest first:

1. Exported `BLITZE_*` environment variables.
2. The project directory's `.env`.
3. The `[blitzecdn]` table in `blitzecdn.toml`.
4. Model defaults.

An explicitly supplied `project_dir` takes precedence over `BLITZE_PROJECT_DIR`.
Constructing a settings model directly validates its arguments without reading
the environment. Existing environment names and flat TOML keys remain valid:

```toml
[blitzecdn]
certificate_renewal_interval_seconds = 43200
backup_dir = "backups"
```

Secrets may come from the environment or `.env`, never TOML. Values remain
masked as `SecretStr`, and only declared capability secrets are forwarded to
Ansible. API credentials are never forwarded. Unknown names and duplicate
ownership are refused during composition.

A blank non-secret capability value selects its model default. It still takes
precedence over lower-priority sources. Zero intervals disable scheduled jobs.
Relative capability paths resolve under the controller's state directory.
Backup location is machine-specific and is excluded from portable restores.
`BLITZE_BACKUP_RETAIN_COUNT` (`backup_retain_count`, default 7) is how many of
the newest archives `blitzecdn backup prune` keeps; 0 keeps all of them. The
daily schedule is a host timer, not an application setting; see
[Backups](operations.md#backups).

## Adding a built-in setting

Add a Pydantic field to the owner's `ConfigurationModel`:

```python
renewal_budget_seconds: int = Field(
    default=300,
    ge=30,
    le=7200,
    alias="BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS",
    description="Wall-clock budget for one certificate renewal sweep.",
)
```

The model owns defaults, types, bounds and normalization. Its
`configuration(feature_name)` declaration supplies ownership and introspection
metadata automatically. Composition validates the model once; services receive
that same typed object through dependency injection. Callers holding scoped
configuration use `config.as_model(CertificateConfig)`.

Use `SecretStr` for secrets. A field's `json_schema_extra` can declare
`minimum_bytes` for a secret, or `portable=False` for a machine-specific setting.
The existing `CapabilitySetting` / `EnvironmentKey` declarations remain
supported for extensions.

Backup configuration can be resolved independently of the database, so recovery
commands continue working when the database is unavailable.

## Environment-file format

The application, host command runner and backup validation share
`core/domain/environment.py`. Files contain one shell-quoted `NAME=value`
assignment per line; `export` is optional. Quote spaces and literal `#` characters.
Variables are not expanded, empty values are empty strings, and the first
assignment of a repeated name wins. Invalid lines fail with a source and line
number without including credential values. The application and host runner
refuse environment-file symlinks.

Backups retain the original assignment spelling while validating with the same
parser, so restoring credentials preserves quoting and literal values.
