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

## Status

Deployments still render on the edge. Shipping the bundle as the sealed
candidate — the edge copying it, checking the manifest, validating and
activating it — is the next step and is not implemented yet.

## Consequences

`tests/contract/test_configuration_rendering.py` runs the real edge roles in
check mode and compares their output with the controller's bundle byte for
byte, in both directions: every predicted path is pre-created so check mode
names it, and a template rendered to a path the controller did not predict
fails the test. Plain, disabled, existing-certificate, uploaded-certificate,
fleet-setting, Under Attack and HTTP/3 configurations are covered. A change to
a template, a default or the recipe that breaks parity fails there.

The renderer depends on ansible-core internals that are not a public API;
ansible-core is pinned to one minor version, and the parity test is what a
version bump has to pass.

## Code and verification

- `core/ansible/rendering.py`, `core/domain/configuration.py`.
- `core/ansible/contributions.py`: one definition of the contribution
  extra-vars, shared by the edge play and the renderer.
- `capabilities/releases/service/releases.py`, `cli.py`: `render`.
- `capabilities/security/nginx/under-attack.js.j2` and the role's
  `under-attack-key.conf.j2`; `paths.secrets` in `blitzecdn_edge`, its
  mounts in `blitzecdn_edge_stack`, `blitzecdn_nginx` and
  `blitzecdn_config_release`.
- `tests/contract/test_configuration_rendering.py`: parity.
- `tests/capabilities/security/test_under_attack.py`: the module under Node,
  including failing closed without a key.
