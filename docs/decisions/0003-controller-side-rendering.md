# 0003: Render edge configuration on the controller, once per release

Status: Accepted

## Context

Edge Nginx configuration is produced from Jinja templates owned by several
capabilities, fed by the release document. If each edge rendered its own
copy during the play, what an edge ran would depend on that edge's Ansible
run. Nobody could see the result before deploying it, diff it against what
the fleet runs, or prove that two edges got the same bytes.

## Decision

`ReleaseService.prepare` renders a release's configuration once, on the
controller, right after the release is compiled and stored, and keeps the
bundle with the release (`core/ansible/rendering.py`,
`capabilities/releases/service/releases.py`).

- Rendering uses ansible-core's own templar, so filters, lookups and variable
  precedence are identical to the edge play's: role defaults in load order,
  the edge group's variables, what `blitzecdn_config_release` binds, then the
  extra-vars.
- The release document and contributions are rendered as data, never as
  trusted templates, so an operator value containing `{{ ... }}` is written
  literally.
- The edge stages those bytes and renders nothing. Host state, such as
  certificates, the ACME webroot and credentials, stays with the edge play.
- Rendering happens outside the write transaction, because it takes seconds.
  If it is interrupted, the next preparation fills it in, and a deployment
  refuses to ship a release without one.

## Consequences

- `blitzecdn release diff` and `deployment diff` show the exact configuration
  a deployment changes, before or after it runs.
- Every edge in a release receives the same configuration tree.
- The controller depends on ansible-core's template API. Compatibility is
  isolated in `core/ansible/templating.py` and checked by the tests named in
  the [README](../../README.md#code-map-and-boundaries).
- The controller must mirror the edge play's variable precedence. A change to
  role order or defaults in the play has to be reflected in `EdgeRenderer`.
