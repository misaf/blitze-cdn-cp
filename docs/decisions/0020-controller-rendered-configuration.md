# Controller-rendered edge configuration

## Context

Edges render their Nginx configuration themselves: the edge play templates the
candidate tree from the release document, and the edge seals a manifest of
whatever it produced. The controller never knows the bytes an edge will serve
in advance, so "this release is what the edge runs" rests on the edge's own
account of it. Two inputs made moving that rendering harder than moving the
templates. Fleet settings were not part of a release — fixed in
[decision 0007](0007-releases-and-reconciliation.md) — and the Under Attack
njs module had the fleet signing secret compiled into it, so any bundle
containing it would carry a credential.

## Decision

`core.ansible.rendering.EdgeRenderer` renders a release's execution document
into a `ConfigurationBundle`: the files and links of the candidate tree, laid
out as `/etc/blitzecdn/releases/<release digest>/`, with a manifest in the
format the edge seals with. It uses ansible-core's own templar over the shipped
role and capability templates, and assembles variables in the edge play's
precedence: role defaults in play order, the edge group's shipped variables,
the candidate binding `blitzecdn_config_release` makes, then the extra-vars.

The release digest is rendered into the configuration — every response reports
it, which is how activation is verified — so a bundle is a projection of a
release, not part of its identity. Its own digest is the digest of its
manifest.

The release document is rendered as **data**. The edge play loads it with
`-e @file`, which Ansible trusts as templates, so an operator-authored value
containing Jinja syntax would be evaluated on the edge; the controller renders
it literally.

A capability ships executable njs as `NginxContribution.module_fragments`,
rendered into the runtime's modules directory. The Under Attack module no
longer contains its key: the security role writes the key, base64-encoded, as a
`js_var` include in the runtime contract's root-only `paths.secrets`
directory, which both the serving and the validation container mount
read-only. The module reads it per request and fails closed without it. Tokens
signed before the change still verify, because the key bytes are unchanged.

`blitzecdn release render --output DIR` writes a release's tree and manifest
for review.

## Shipping the rendering

`ReleaseService.prepare` renders a release once and stores the bundle in
`release_configurations`, verified against its digest on every read. Rendering
happens outside the write transaction and only when nothing is on record, so
unchanged state prepared on every drift check is not rendered again. A
deployment's PREPARE phase writes the stored bundle beside the execution
document and names it there, outside the artifact hash, so a rollback ships the
bytes the release first produced whatever the templates have since become.

On the edge, `blitzecdn_config_release` ships the bundle and its stdlib helper
stages it: an unsealed candidate is replaced, file and link entries are
checked against the bundle's own manifest, and the tree on disk must equal the
controller's manifest before the bundle is removed. A sealed candidate is
verified and must be the same bundle. The candidate is then tested with
`nginx -t` under the selected image, sealed with its runtime image, and
activated as before. `blitzecdn_nginx` renders nothing; it installs host state.

Drift detection remains a check-mode run: an edge whose candidate for this
release is not staged reports the staging as a change.

## Consequences

Before the edge stopped rendering, a test ran the real edge roles in check mode
and matched the controller's bundle byte for byte, in both directions, across
plain, disabled, existing-certificate, uploaded-certificate, fleet-setting,
Under Attack and HTTP/3 configurations (commit `4c69908`). The controller is now
the only renderer. What an edge serves is the stored bundle, verified on the
controller, on the edge before staging, after staging, at sealing and at
activation; `nginx -t` under the serving image still gates every candidate.

The template-injection path through `-e @file` is closed: no edge templates the
release document any more.

The renderer depends on ansible-core internals that are not a public API, and
ansible-core is pinned to one minor version. A bump can change rendered bytes:
releases already prepared keep theirs, and a new one should be compared with
`blitzecdn release render` before it is deployed.

## Code and verification

- `core/ansible/rendering.py`, `core/domain/configuration.py`.
- `core/ansible/contributions.py`: one definition of the contribution
  extra-vars, shared by the edge play and the renderer.
- `capabilities/releases/service/releases.py`, `cli.py`: `render`.
- `capabilities/security/nginx/under-attack.js.j2` and the role's
  `under-attack-key.conf.j2`; `paths.secrets` in `blitzecdn_edge`, its
  mounts in `blitzecdn_edge_stack`, `blitzecdn_nginx` and
  `blitzecdn_config_release`.
- `capabilities/releases/adapters`: the `release_configurations` record.
- `capabilities/deployments/composition.py`: `publish_release`.
- `ansible/roles/blitzecdn_config_release/tasks/stage.yml` and the helper's
  `stage`; `blitzecdn_nginx` reduced to host state.
- `tests/contract/test_configuration_rendering.py`: prepared releases keep
  their rendering; `tests/contract/roles/test_configuration_transactions.py`:
  staging, tampering, retries and drift reporting with real Ansible.
- `tests/capabilities/security/test_under_attack.py`: the module under Node,
  including failing closed without a key.
