# Changelog

What changed between releases, for operators and API clients. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/).

Add a line under **Unreleased** in the same pull request as the change it
describes. `just release-version X.Y.Z` turns that section into the release's
and refuses to run while it is empty. Numbers in parentheses are pull requests.

## [Unreleased]

### Added

- Optimistic concurrency for zones, records, rules and edges. Single-resource
  reads and writes return a strong `ETag`; `PATCH` and `DELETE` accept
  `If-Match` and answer `412` without writing when the resource has changed
  since that version. Without `If-Match`, writes behave as before (#380).
- Every queued resource (`Operation`, `Deployment`, `Workflow`) starts with
  the same fields: `id`, `kind`, `status`, `done`, `created_at`,
  `finished_at`, `error` and `result`. `done` says when to stop polling
  without knowing the resource's status values; deployments gain `kind`
  (`deployment` or `rollback`) and workflows gain `finished_at` (#373).
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
- Canary-first rollouts that continue in batches (#321). `just bench-rollout` (#357)
  measures run counts, failure blast radius and controller cost per batch size
  and models durations, and a timing shim measures real disposable edges; the
  default batch size stays 10 (`docs/design/rollout-benchmark.md`).
- Automatic recovery from a failed control-plane update (#212).
- Install-time verification of the control-plane image, installer and Docker
  key (#221).
- `SECURITY.md` with a private reporting channel (#303).
- `CONTRIBUTING.md`, this changelog, and the OpenAPI document published as
  `docs/openapi.json`.

### Changed

- `POST /v3/domains` takes a `DomainCreate` body and `POST /v3/edges` an
  `EdgeCreate` body, so every resource is created with `XCreate`, updated
  with `XPatch` and read back as `X`. The fields are unchanged; `Domain` and
  `Edge` are now only response models (#381).
- `GET /v3/dns/export` is now `GET /v3/domains/export`, with
  `GET /v3/domains/{domain}/export` for one zone, and both return the named
  `DnsExport` model. A proxied record's `value` is `null` rather than omitted.
  `blitzecdn dns export` is now `blitzecdn domain export [NAME]` (#377).
- `docs/openapi.json` declares each operation's error responses, `401`,
  `403`, `404`, `409`, `413` and the rest, with an `ErrorResponse`
  schema for their `{"detail": "message"}` body, so generated clients expect
  them (#375).
- Cache statistics and collection freshness moved under `/v3/traffic`:
  `GET /v3/cache/stats` is now `GET /v3/traffic/cache`, and
  `GET /v3/traffic/status/{edge}` is now `GET /v3/traffic/collection`, a page
  of every edge's status that `?edge=` narrows to one (#378).
- The API has one noun per resource and one prefix per area (#289).
- The `dns` capability is now `sites` (#302), and Automatic SSL is its own
  capability (#227).
- `blitzecdn init` is folded into `blitzecdn setup --env-file` (#204).
- Certificates are issued in-process with the `acme` library instead of
  certbot (#298); issuance, renewal and reconciliation run on the worker
  (#224).
- `POST /v3/ssl/automatic/reconcile` is queued and answers `202` with an
  `Operation`; the scan is its `result` (#361).
- Automatic SSL/TLS reconciliation moves to
  `POST /v3/certificates/ssl-modes/reconcile`, so the fleet's TLS work has one
  prefix; `/v3/ssl/...` is gone, with no alias (#376).
- Certificate DNS preflight resolves at the authoritative nameservers (#142)
  and blocks a hostname that also resolves off-edge (#141). HTTP-01 challenges
  are placed only on edges the hostname resolves to (#233).
- GeoLite2 is downloaded once on the controller and shipped to edges (#127).
- Origin connections are pooled in keepalive upstream groups (#118).
- The server lifecycle is image-only (#93); Docker setup lives only in the
  `blitzecdn_docker` role (#95), and the host Docker SDK runner is replaced by
  Compose (#107).
- The control plane requires Python 3.15.2 (#203).
- Rollback starts from the release the fleet runs, not desired state (#309).
- The release diff names its baseline as `baseline_release` (was
  `live_release`) and reports each edge's recorded release, grouped, with
  warnings when the fleet is mixed, unverified or empty (#343).
- `audit_retention` is an operator setting (#304).
- `/metrics` uses `prometheus-client` (#295), and logs carry structured
  keyword fields (#297).
- Every artefact is licensed under MIT (#287).

### Fixed

- The certificate requirement is cleared only after a full forward rollout
  (#84), and Flexible is served as soon as an Auto host's certificate
  activates (#126).
- CLI deployments no longer fail on concurrent writers or a busy lock (#88).
- Long deployments renew their job lease and are not taken over (#239).
- A deployment whose job kills its worker on every attempt is finished as
  `abandoned` with the job, not left `running` until a restart re-queues it
  (#334).
  Interrupted convergence and workflows are left to takeover recovery (#324).
  A worker that stalled past its lease and finishes after a takeover no longer
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
- `allow_sources` is exempt from country and method rules (#131), and
  firewall path rules match regardless of letter case (#206).
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
- Edge host and user values are restricted to safe characters (#293), and
  every field that reaches Nginx configuration passes one typed validation
  layer (#320).
- Ansible extra-vars strings are tagged `!unsafe` (#326).
- `/.blitzecdn/release` answers only the controller, with a signed probe
  (#201, #312).
- Edges turn off `server_tokens` (#199) and redact query strings and client
  IPs in access logs (#202).
- The Under Attack challenge page's CSP nonce is derived per response (#308).
- The controller's loopback SSH key is restricted (#237).

## [6.1.0] - 2026-10-27

### Added

- Controller operational status and metrics.
- Structured job execution and recovery logs.
- Developer onboarding and architecture guide.

### Changed

- Capability configuration is defined in typed models.
- Ansible templating compatibility is isolated in one module.
- The HTTP/3 integration harness converges the edge through the shipped play
  and a real release.

### Fixed

- A Flexible origin is reached on port 80 behind HTTPS/443.
- Security audit findings and HTTP/3 harness failures.

## Earlier releases

Releases v2.4.1 to v6.0.1 have no notes in this file. Their changes are in the
git history between the release tags.

[Unreleased]: https://github.com/misaf/blitze-cdn-cp/compare/v6.1.0...HEAD
[6.1.0]: https://github.com/misaf/blitze-cdn-cp/compare/v6.0.1...v6.1.0
