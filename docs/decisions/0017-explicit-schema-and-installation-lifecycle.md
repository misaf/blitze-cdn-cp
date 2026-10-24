# Explicit schema and installation lifecycle

## Decision

Schema changes are a maintenance operation. `Database` and `Repository`
construction validate an existing database through a read-only SQLite connection;
they do not create directories, database files, migration locks or tables. Alembic's
revision and metadata comparison still refuse unknown, unprepared and damaged
schemas before services use their stores. Runtime connections retain their
existing transaction, foreign-key and WAL behavior. They open in read/write
mode without permission to create a replacement database if the file disappears.
SQLite may manage its normal WAL sidecars when opening an existing WAL database.

`DatabaseSchema.migrate(path)` owns directory preparation, cross-process locking,
upgrade to head and metadata validation. An in-process lock protects Alembic's
global environment proxy during validation and migration. Engines are disposed
also when validation fails. SQLite URI paths and Alembic interpolation preserve
spaces, percent signs and query/fragment characters in database filenames.

`blitzecdn migrate` exposes this operation without constructing the application,
loading runtime plugins or creating credentials. `blitzecdn setup` explicitly
migrates and then creates a missing local `.env`, preserving existing credentials.
The hidden `setup --schema-only` path is removed. Errors direct the operator to
stop services, preserve a backup, migrate or restore; they never advise deleting
controller state.

Backup restore continues to call the same explicit migration operation on its
staged database before publication. It no longer depends on constructor side
effects. The schema remains the single `0001` revision with no data-shape changes.

## Installation

The managed role builds the application image, verifies container SSH access,
stops the API and worker, runs `blitzecdn migrate` in a one-shot CLI container,
and only then recreates services and checks their health. This ordering also
applies when the role is run directly for repair. Existing update and upgrade
commands retain their backup and stop boundaries.

All capabilities now ship in one distribution. The installer no longer accepts
`BLITZECDN_CAPABILITIES` or passes a configuration-selection list to the role.
The corresponding role argument is removed. The managed TOML always includes
backup and TLS settings, and a new managed installation requires its ACME email.
Third-party configuration ownership validation is unchanged.

## Verification

Lifecycle tests exercise missing, unprepared, damaged and unknown schemas without
changing their database bytes; constructor startup cannot call upgrade. Explicit
migrations are repeatable and separate processes can initialize one database
concurrently. Application fixtures explicitly migrate their test databases.

The installed-wheel test runs outside the checkout: an ordinary command refuses
an absent database, `migrate` initializes it without credentials, stores are
usable, and repeated setup preserves both credentials and fleet state. Existing
backup recovery, application, API, worker and installation checks run together.

CLI snapshots intentionally add `migrate` and remove `setup --schema-only`;
the Ansible snapshot removes the capability-selection argument. HTTP, extension
ABI/SDK and database snapshots are unchanged. Real Ubuntu edge convergence and
recovery require their own subsequent verification step.
