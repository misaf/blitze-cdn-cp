# Changelog

What changed between releases, for operators and API clients. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/).

Add a line under **Unreleased** in the same pull request as the change it
describes. `just release-version X.Y.Z` turns that section into the release's
and refuses to run while it is empty. Numbers in parentheses are pull requests.

## [Unreleased]

### Changed

- The edge image is built on Nginx 1.31.7 (from 1.31.4) (#399).
- The control plane runs on SQLAlchemy 2.1.8 (from 2.0.52), and the package
  accepts SQLAlchemy up to 2.1 (`sqlalchemy>=2.0.52,<2.2`) (#391).

## [7.0.0] - 2026-11-13

BlitzeCDN 7.0.0 makes the v3 API consistent (one noun per resource, one prefix
per area, explicit OpenAPI operation ids) and adds optimistic concurrency, a
shared shape for queued work, and a long list of operator features.

> [!IMPORTANT]
> **7.0.0 is a fresh install. There is no upgrade path from 6.x or any earlier
> version.** Install it on a new control plane and edges with `install.sh`,
> since existing installs and their databases cannot be carried forward. From
> 7.0.0 onward, installs upgrade in place to later 7.x releases and to future
> majors.

The **BREAKING** changes matter to API clients and scripts written against 6.x.

### Added

- Every `201 Created` response sends `Location` with the new resource's path
  (`POST /v3/domains`, `/v3/domains/{domain}/records`,
  `/v3/domains/{domain}/rules` and `/v3/edges`), as `202` responses already
  did, and OpenAPI declares the header (#383).
- Optimistic concurrency for zones, records, rules and edges. Single-resource
  reads and writes return a strong `ETag`; `PATCH` and `DELETE` accept
  `If-Match` and answer `412` without writing when the resource has changed
  since that version. Without `If-Match`, writes behave as before (#380).
- Every queued resource (`Operation`, `Deployment`, `Workflow`) starts with the
  same fields: `id`, `kind`, `status`, `done`, `created_at`, `finished_at`,
  `error` and `result`. `done` says when to stop polling without knowing the
  resource's status values; deployments gain `kind` (`deployment` or
  `rollback`) and workflows gain `finished_at` (#373).
- Daily scheduled backups with retention and freshness metrics (#86), encrypted
  to age recipients when written (#313). `--identity` also takes an RSA key in
  the PEM form `ssh-keygen -m PEM` writes, as the `age` tool does, and the
  archives are checked against the pinned reference `age` CLI.
- Per-site firewall `allowed_methods` and `allowed_paths` (#129), per-site rate
  limiting (#208), and a configurable Under Attack proof-of-work difficulty
  (#133).
- Edge-wide visitor timeouts and a per-address connection limit (#211).
- Per-site cache levels and cookie bypass (#235), edge TTL overrides and
  ignored origin headers (#236), and hourly cache statistics stored with
  traffic accounting (#137).
- Cache purge by hostname, path prefix (#220) and tag (#222); purging a named
  entry removes its Vary variants (#301). What a purge guarantees about entries
  nginx stores, replaces or evicts while it runs is documented and tested
  against a real nginx (#344).
- Path-based rules that match by hostname and path (#238).
- Per-site error pages and maintenance mode (#241).
- Per-site analytics in the API and CLI (#210).
- Alerts for failing deployments and drift checks (#140).
- Canary-first rollouts that continue in batches (#321). `just bench-rollout`
  (#357) measures run counts, failure blast radius and controller cost per
  batch size and models durations, and a timing shim measures real disposable
  edges; the default batch size stays 10 (`docs/design/rollout-benchmark.md`).
- Automatic recovery from a failed control-plane update (#212).
- Install-time verification of the control-plane image, installer and Docker
  key (#221).
- `SECURITY.md` with a private reporting channel (#303).
- `CONTRIBUTING.md`, this changelog, and the OpenAPI document published as
  `docs/openapi.json`.

### Changed

- **BREAKING:** `docs/openapi.json` groups operations by tag (`domains`,
  `certificates`, `deployments`…) and gives each a stable, explicit
  `operationId` such as `domains_list` or `cache_purge`, in place of ids
  derived from the path. The uploaded-certificate body schema is renamed to
  match. `/health` and the release artifact route publish response models,
  `expiring_in` on `GET /v3/certificates` says it counts days, and operations
  that read no input no longer declare a `422` (#370).
- **BREAKING:** Path parameters are named after their resource, and one
  resource has one name in every path: `/v3/sites/{site}`, `/v3/edges/{edge}`,
  `/v3/domains/{domain}/rules/{rule}`,
  `/v3/domains/{domain}/records/{type}/{record}` and
  `/v3/releases/{release_id}/artifacts/{artifact}` replace `{name}`. URLs are
  unchanged; generated clients rename their arguments. `host_limit` on
  `GET /v3/releases/current` is documented as the edge selector it is
  everywhere, not pagination (#382).
- **BREAKING:** `POST /v3/domains` takes a `DomainCreate` body and
  `POST /v3/edges` an `EdgeCreate` body, so every resource is created with
  `XCreate`, updated with `XPatch` and read back as `X`. The fields are
  unchanged; `Domain` and `Edge` are now only response models (#381).
- **BREAKING:** `GET /v3/dns/export` is now `GET /v3/domains/export`, with
  `GET /v3/domains/{domain}/export` for one zone, and both return the named
  `DnsExport` model. A proxied record's `value` is `null` rather than omitted.
  `blitzecdn dns export` is now `blitzecdn domain export [NAME]` (#377).
- `docs/openapi.json` declares each operation's error responses, `401`, `403`,
  `404`, `409`, `413` and the rest, with an `ErrorResponse` schema for their
  `{"detail": "message"}` body, so generated clients expect them (#375).
- **BREAKING:** Cache statistics and collection freshness moved under
  `/v3/traffic`: `GET /v3/cache/stats` is now `GET /v3/traffic/cache`, and
  `GET /v3/traffic/status/{edge}` is now `GET /v3/traffic/collection`, a page
  of every edge's status that `?edge=` narrows to one (#378).
- **BREAKING:** The API has one noun per resource and one prefix per area
  (#289).
- **BREAKING:** The `dns` capability is now `sites` (#302), and Automatic SSL
  is its own capability (#227).
- Certificates are issued in-process with the `acme` library (#298); issuance,
  renewal and reconciliation run on the worker (#224).
- **BREAKING:** Automatic SSL/TLS reconciliation is queued: it answers `202`
  with an `Operation` whose `result` is the scan (#361), and moves to
  `POST /v3/certificates/ssl-modes/reconcile`, so the fleet's TLS work has one
  prefix (#376).
- Certificate DNS preflight resolves at the authoritative nameservers (#142)
  and blocks a hostname that also resolves off-edge (#141). HTTP-01 challenges
  are placed only on edges the hostname resolves to (#233).
- GeoLite2 is downloaded once on the controller and shipped to edges (#127).
- Origin connections are pooled in keepalive upstream groups (#118).
- **BREAKING:** The server lifecycle is image-only (#93); Docker setup lives
  only in the `blitzecdn_docker` role (#95), and the host Docker SDK runner is
  replaced by Compose (#107).
- **BREAKING:** The control plane requires Python 3.15.2 (#203).
- Rollback starts from the release the fleet runs, not desired state (#309).
- **BREAKING:** The release diff names its baseline as `baseline_release` (was
  `live_release`) and reports each edge's recorded release, grouped, with
  warnings when the fleet is mixed, unverified or empty (#343).
- `audit_retention` is an operator setting (#304).
- `/metrics` uses `prometheus-client` (#295), and logs carry structured keyword
  fields (#297).
- Every artefact is licensed under MIT (#287).

### Removed

- **BREAKING:** `/v3/ssl/...`, with no alias; Automatic SSL/TLS reconciliation
  is at `POST /v3/certificates/ssl-modes/reconcile` (#376).
- **BREAKING:** `GET /v3/dns/export` (#377), `GET /v3/cache/stats` and
  `GET /v3/traffic/status/{edge}` (#378), with no aliases; their replacements
  are listed under Changed.
- **BREAKING:** `blitzecdn init`; use `blitzecdn setup --env-file` (#204).
- certbot. Certificates are issued with the `acme` library (#298).

### Fixed

- The certificate requirement is cleared only after a full forward rollout
  (#84), and Flexible is served as soon as an Auto host's certificate activates
  (#126).
- CLI deployments no longer fail on concurrent writers or a busy lock (#88).
- Long deployments renew their job lease and are not taken over (#239).
- A deployment whose job kills its worker on every attempt is finished as
  `abandoned` with the job, not left `running` until a restart re-queues it
  (#334).
- Interrupted convergence and workflows are left to takeover recovery (#324). A
  worker that stalled past its lease and finishes after a takeover no longer
  overwrites its workflow's `needs_review` with `failed`, so the review stays
  in the queue.
- A queued request's `Idempotency-Key` is bound in the transaction that queues
  its work, so a control plane that stops before answering cannot let a retry
  queue the work twice (#336).
- Old edge configuration releases and deployments are pruned (#104, #135).
- Superseded TLS keys are removed from edges (#123).
- Every step of ACME issuance (CA requests and challenge plays) is stopped at
  what is left of the issuance budget, and challenge cleanup has a bounded
  allowance inside it, so a stalled CA or edge cannot hold the deployment lock
  past the deployment timeout (#347).
- Edge SSH host keys survive control-plane updates (#136).
- The origin's `Accept-Encoding` is normalised without the cache (#128).
- `allow_sources` is exempt from country and method rules (#131), and firewall
  path rules match regardless of letter case (#206).
- The firewall and rate limit apply before a path-rule redirect (#300).
- `firewall.case_insensitive_paths` on its own makes `rate_limit.paths` match
  regardless of letter case (#372).
- A proxied hostname may have both an A and an AAAA record (#311).
- Nginx name hashes are sized for the longest names (#216).
- Disabled sites are skipped in serving verification (#307).
- Rejected traffic log records are skipped and counted instead of stalling the
  file (#310).
- Each request is written to one access log, and `error.log` rotates (#110).

### Security

- Non-ASCII API keys get 401 instead of 500 (#87), and request bodies are
  limited before parsing (#105).
- The example environment no longer ships a working API key (#90), and `.env`
  is created at mode 0600 atomically (#191).
- Origins that point an edge at itself are refused (#124), as are IPv6 zone
  indexes in firewall and edge sources (#292, #317) and edge firewall sources
  that leave the controller out (#318).
- Edge host and user values are restricted to safe characters (#293), and every
  field that reaches Nginx configuration passes one typed validation layer
  (#320).
- Ansible extra-vars strings are tagged `!unsafe` (#326).
- `/.blitzecdn/release` answers only the controller, with a signed probe (#201,
  #312).
- Edges turn off `server_tokens` (#199) and redact query strings and client IPs
  in access logs (#202).
- The Under Attack challenge page's CSP nonce is derived per response (#308).
- The controller's loopback SSH key is restricted (#237).

## [6.1.0] - 2026-10-27

6.1.0 adds controller visibility and strengthens configuration, rendering and
transaction handling. Existing environment variable names and flat TOML
settings remain supported, and worker heartbeat records need no schema
migration: upgrade with the normal installer update, which migrates the
database and keeps persistent state. The release publishes
`ghcr.io/misaf/blitzecdn-control-plane:6.1.0` and
`ghcr.io/misaf/blitzecdn-edge:6.1.0`.

### Added

- Controller operational status and metrics: `blitzecdn overview`, an
  authenticated `/v1/status`, worker heartbeats, and queue, workflow and
  certificate metrics.
- Structured job execution and recovery logs, with warnings for interrupted
  workflows.
- Developer onboarding and architecture guide.

### Changed

- Capability configuration is defined in typed models, and environment-file
  parsing is shared by the application, host and backup paths.
- Transaction ownership is explicit, and host configuration is read from one
  database snapshot.
- Ansible templating compatibility is isolated in one module.
- The HTTP/3 integration harness converges the edge through the shipped play
  and a real release.

### Fixed

- A Flexible origin is reached on port 80 behind HTTPS/443.
- Security audit findings and HTTP/3 harness failures.

## [6.0.1] - 2026-10-26

### Added

- Servers install from the published control-plane image.
- Edge operations run as queued worker jobs, and `job show` prints an
  operation's result.

### Changed

- The `plugins` command is renamed `features`.
- Settings load through `pydantic-settings`, and the application is composed
  with `dishka`.
- The source tree is regrouped: processes under `entrypoints/` and `host/`,
  shipped trees under `assets/`, and providers merged into `capabilities/`.
  The installer is slimmed to a bootstrap plus host helpers.

### Fixed

- The release selector runs `git` by its resolved path.
- The inventory finds the project root for its default database.

### Security

- Locked packages with published advisories are upgraded.

## [6.0.0] - 2026-10-25

### Added

- Edge configuration is rendered on the controller and shipped in each
  release, and release inputs capture fleet settings.
- Deployments stage sealed configurations and activate them with durable
  rollback, persist the admitted target set before queueing, resume
  interrupted work from durable checkpoints, and verify the active release
  through the public listener.
- Certificate material is content-addressed and retained for reproducible
  rollback; unreferenced material is collected.
- A verified controller operating and recovery guide.

### Changed

- All capabilities are consolidated into one application distribution, with
  one capability roster and per-process composition.
- Schema migration is an explicit step; startup no longer migrates.
- Release artifacts are immutable and their stored identity is verified.
- The top-level documents and decision records are removed.

### Fixed

- A job whose attempts were spent by its worker dying is failed, and a
  deployment its worker died in is abandoned when the job returns.
- Jobs are claimed under `BEGIN IMMEDIATE`, duplicate enqueues are refused,
  and scheduled jobs run under their schedule's lease and publish atomically
  with their firing.
- A submitted deployment and its job are committed together; busy
  deployments are deferred without spending retries, and queued job outcomes
  match deployment results.
- Verification fails for an edge presenting the wrong certificate.
- Restores migrate the database before publishing live state, recover
  previous components after a partial restore, and keep backup directories
  when rollback fails.
- Applied deployments are found beyond recent history.
- The shipped example no longer sets `redis_url`, which refused to start.

### Security

- A play receives only the `BLITZE_*` environment names it needs.
- IPv6 zone indexes are refused in hostnames.

## [5.0.0] - 2026-09-10

### Added

- Content-addressed releases, and per-edge rollouts with five-phase targets
  and a serving probe. Deployments hold a release reference instead of an
  embedded snapshot.
- A durable SQLite job queue.
- Edges declare the runtime capabilities they provide.
- The installer names the API access list at install time and repoints it on
  a release move; uninstall withdraws the API firewall rules; diagnostics
  report who the API answers.

### Changed

- Zone policy: the policy lives on the zone and exceptions in rules; the
  virtual host is derived rather than stored, and a record's value is the
  origin.
- Certificates moved under `/v1/hosts`.
- `--yes` means only "do not prompt", and `--json` output has one definition.
- Both delivery surfaces share one failure taxonomy.
- Every distribution ships `py.typed`.

### Removed

- Redis, Dramatiq and APScheduler, replaced by the SQLite job queue.

### Fixed

- A host the derivation cannot build is dropped instead of failing the fleet.
- `Settings` is authoritative for configuration defaults.
- The installer refuses a standalone host that cannot be an edge.

### Security

- An authorized request is never cached or served to the next visitor.
- Certificate paths are validated in one place, blocking Nginx directive
  injection.
- An operator-set issuer mode is refused.

## [4.1.0] - 2026-09-08

### Added

- Opt-in remote API access from an allowlist of client addresses, admitted by
  transport peer (never by header), with the controller firewall converged
  from the same list.

### Security

- IPv6 addresses the API cannot admit, and CIDRs with host bits set, are
  refused.

## [4.0.1] - 2026-09-08

### Changed

- Backups and restores go through the Docker Python SDK, restore
  configuration and recover control-plane services safely, and wait for the
  plane to come back before reporting done.

### Fixed

- Convergence no longer deletes the Nginx fragments it just rendered, and the
  stack preview shows the image the converge would run.
- Edges own the cache directory as the writer's uid and let the worker read
  ACME challenges.
- Every operator switch passed on the command line is coerced to its type.
- Every playbook gets the collections, and the controller gets the
  certificates it is asked to configure.
- A renewal renews the certificate.

## [4.0.0] - 2026-09-06

### Added

- Per-site maximum upload size.
- Site settings the CLI could not previously reach.
- Major-version upgrades in the installer.
- A written statement of the project's public surfaces, frozen by tests.

### Changed

- 4.x has its own release line.
- The audit table is bounded.
- Ansible roles are named for the wheel that ships them.

## [3.0.0] - 2026-09-04

### Added

- Optional capabilities shipped as separate distributions, each with its own
  Ansible role, Nginx resources and settings: backup, cache, certificates and
  Automatic SSL, compression, security, HTTP/3, GeoIP, hardening, origins and
  resolver.
- The edge image is built from the modules the installed capabilities declare;
  `edge image spec` prints it.
- A canonical site model in the CLI and API.

### Changed

- The control plane is reorganised into a core module and capability packages.
- The two API versions are collapsed into one.
- 3.x has its own release branch.

### Fixed

- A second proxied record for one hostname is refused.
- An invalid edge security deployment is rejected early.

## [2.10.0] - 2026-08-31

### Added

- Edge rollouts start with a single canary edge.

### Changed

- The edge runtime is rebuilt on the official Alpine Nginx image, with the
  health check in Compose and mandatory.
- The Docker daemon configuration is validated against `dockerd` before
  deploying.
- The control-plane Compose project is named, and the controller has its own
  SSH configuration.

## [2.9.1] - 2026-08-30

### Changed

- The control plane runs as the image's `nobody:nogroup` identity.
- The README is trimmed to a quick start.

## [2.9.0] - 2026-08-30

There is no 2.8.0: the version went from 2.7.0 straight to 2.9.0, and 2.8.0
was never released.

### Changed

- The control plane runs as dedicated Docker Compose containers, and restore
  goes through the Compose wrapper.
- Edges need a fresh host; an in-place native Nginx is no longer migrated.

## [2.7.0] - 2026-08-30

### Added

- API v2, alongside the preserved v1.
- HTTP/3, response compression, and an Under Attack Mode browser challenge at
  the edge.
- The origin is told who the visitor is.
- One command group backs up and restores the control plane.

### Changed

- Edges run as Docker containers instead of native packages.
- Origin requests are routed by visitor scheme and listener port rather than
  deriving the origin port from the edge listener.

## [2.6.1] - 2026-08-25

### Fixed

- Automatic SSL runs after a host's first certificate is issued.

## [2.6.0] - 2026-08-25

### Added

- Automatic SSL/TLS: origin scanning and fleet reconciliation.

## [2.5.0] - 2026-08-24

### Fixed

- Edge ports are preserved when proxying to origins.

## [2.4.1] - 2026-08-24

Releases 2.2.0 to 2.4.0 were published on GitHub without tags in this
repository; their changes are included here.

### Added

- A combined `ssl_mode` per record (`record ssl`) replacing `origin_scheme`.
- An "Always Use HTTPS" toggle independent of the SSL mode.
- Cloudflare-compatible public proxy ports in the firewall and Nginx.

### Removed

- The `origin_scheme` compatibility layer.

### Fixed

- Rolling upgrades across the HTTPS-redirect schema change.

## [2.1.0] - 2026-08-16

### Added

- Long-running work moves to a worker, and scheduled operations run once,
  durably.
- Health, Prometheus metrics and structured logs on the controller.
- `blitzecdn db backup`, an uninstall role and playbook, and a
  state-preserving update path in the installer.
- Origin probes from the edges.
- Distinct CLI exit codes mirroring the API's status mapping.
- The API key is declared as an OpenAPI security scheme.

### Changed

- Ansible runs through Ansible Runner's Python API, using only pinned
  collections.
- Managed certificates are stored under immutable fingerprint names.
- Check-mode deployments and finished workflows are retained for a bounded
  time.
- Fresh installs stay on the 2.x release branch.

### Fixed

- A rollback is not adopted over a concurrent record write.
- Certificate reconciliation and renewal reporting.
- Cross-process database migrations are serialised.

## [2.0.1] - 2026-08-13

### Changed

- Certificate preflight uses public DNS by default.

## [2.0.0] - 2026-08-12

### Added

- Edges are managed over HTTP and stored in the control-plane database,
  published to Ansible through an inventory plugin.
- Durable workflows, an outbox and database-backed settings.
- A clone-then-install flow replaces the piped `bootstrap.sh`.

### Changed

- The control plane moves to SQLAlchemy, Alembic, `uv` and `just`.
- Service writes are wrapped in one atomic unit of work.

### Fixed

- The installer picks the `uv` build by OS and retries its download.

## [1.7.5] - 2026-08-11

### Fixed

- The install one-liner works again: 1.7.4 had removed the `bootstrap`
  argument it needs.

## [1.7.4] - 2026-08-11

### Changed

- The edge roles are vendored, and a role owns the control-plane host.
- The minimum controller platform is raised.

## [1.7.3] - 2026-08-11

### Added

- `--fresh` and `--uninstall` installer subcommands.

### Changed

- The control plane is restructured around ports and services, with domain
  events and an audit trail subscribing to them.
- Run outcomes are read as structured data, not parsed recap text.

### Fixed

- A drifted `RecordPatch` is refused on import.

## [1.7.2] - 2026-08-10

### Changed

- Edge settings have an untracked home.

## [1.7.1] - 2026-08-10

### Added

- MaxMind credentials are configured from `.env`.

## [1.7.0] - 2026-08-10

### Changed

- Deploys `blitzecdn.edge` 1.8.0, which fetches GeoLite2 itself.

## [1.6.2] - 2026-08-10

### Fixed

- A tightened validator no longer strands an already-stored record.

## [1.6.1] - 2026-08-10

### Fixed

- Country codes ISO does not assign are refused.

## [1.6.0] - 2026-08-10

### Added

- `--version`, and help text for every command.

## [1.5.0] - 2026-08-10

### Added

- Per-hostname request filtering (`blitzecdn.edge` 1.7.0).

## [1.4.0] - 2026-08-10

### Added

- Preflight asks public resolvers and detects a dishonest one.

## [1.3.0] - 2026-08-10

### Changed

- The three installers are merged into `install.sh`.

## [1.2.9] - 2026-08-10

### Changed

- Deploys `blitzecdn.edge` 1.5.0.

## [1.2.8] - 2026-08-10

### Fixed

- Standalone certificate onboarding.

## [1.2.7] - 2026-08-10

### Changed

- Ansible dependencies are upgraded to warning-free versions.

## [1.2.6] - 2026-08-09

### Fixed

- Ansible Git tag upgrades.

## [1.2.5] - 2026-08-09

### Fixed

- Updates from a service-owned checkout.

## [1.2.4] - 2026-08-09

### Added

- A safe standalone release updater.

### Changed

- Release tags must start with `v`.

## [1.2.3] - 2026-08-09

### Fixed

- Standalone ownership repair is limited to what it should touch (#4).

## [1.2.2] - 2026-08-09

### Security

- Installation and deployment safety are hardened (#3).

## [1.2.1] - 2026-08-09

### Added

- A standalone server installer (#2).

## [1.2.0] - 2026-08-09

### Added

- Canary deployments with `--limit`, drift detection, origin checks and
  certificate renewal (#1).

## [1.1.0] - 2026-08-05

First tagged release. There is no 1.0.0: the version went from the untagged
0.1.0 straight to 1.1.0, and 1.0.0 was never released.

### Added

- The Python control plane, with DNS records as the source of truth and sites
  derived from them as virtual hosts.
- Edge inventory management and a one-command installer.
- CLI commands for domains, records, edges and DNS export; `deploy` validates
  and previews first.
- Edge automation in the `blitzecdn.edge` collection.
- A project-local `.env` as low-precedence configuration defaults.

### Security

- SSH between the control plane and edges is public-key only.

[Unreleased]: https://github.com/misaf/blitze-cdn-cp/compare/v7.0.0...HEAD
[7.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v6.1.0...v7.0.0
[6.1.0]: https://github.com/misaf/blitze-cdn-cp/compare/v6.0.1...v6.1.0
[6.0.1]: https://github.com/misaf/blitze-cdn-cp/compare/v6.0.0...v6.0.1
[6.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v5.0.0...v6.0.0
[5.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v4.1.0...v5.0.0
[4.1.0]: https://github.com/misaf/blitze-cdn-cp/compare/v4.0.1...v4.1.0
[4.0.1]: https://github.com/misaf/blitze-cdn-cp/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.10.0...v3.0.0
[2.10.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.9.1...v2.10.0
[2.9.1]: https://github.com/misaf/blitze-cdn-cp/compare/v2.9.0...v2.9.1
[2.9.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.7.0...v2.9.0
[2.7.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.6.1...v2.7.0
[2.6.1]: https://github.com/misaf/blitze-cdn-cp/compare/v2.6.0...v2.6.1
[2.6.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.5.0...v2.6.0
[2.5.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.4.1...v2.5.0
[2.4.1]: https://github.com/misaf/blitze-cdn-cp/compare/v2.1.0...v2.4.1
[2.1.0]: https://github.com/misaf/blitze-cdn-cp/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/misaf/blitze-cdn-cp/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.5...v2.0.0
[1.7.5]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.4...v1.7.5
[1.7.4]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.3...v1.7.4
[1.7.3]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.2...v1.7.3
[1.7.2]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.1...v1.7.2
[1.7.1]: https://github.com/misaf/blitze-cdn-cp/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.6.2...v1.7.0
[1.6.2]: https://github.com/misaf/blitze-cdn-cp/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/misaf/blitze-cdn-cp/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.9...v1.3.0
[1.2.9]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.8...v1.2.9
[1.2.8]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.7...v1.2.8
[1.2.7]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.6...v1.2.7
[1.2.6]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.5...v1.2.6
[1.2.5]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.4...v1.2.5
[1.2.4]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.3...v1.2.4
[1.2.3]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/misaf/blitze-cdn-cp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/misaf/blitze-cdn-cp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/misaf/blitze-cdn-cp/releases/tag/v1.1.0
