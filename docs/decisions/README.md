# Decision log

One page per decision that shapes the code, each in the same form: the
context that forced a choice, the decision, and its consequences, good and
bad. The [architecture walkthrough](../architecture.md) shows where each one
appears in a real request.

These pages record why. The [edge design notes](../design/README.md) record
how the edge templates and playbooks implement a choice, and a decision page
links to the note rather than repeating it. Inline comments keep only what a
reader needs to change that line safely (see the
[comment rule](../design/README.md#comment-rule)).

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-sqlite-and-no-broker.md) | SQLite for state and the job queue; no message broker | Accepted |
| [0002](0002-ansible-push.md) | Push configuration to edges with Ansible over SSH; no edge agent | Accepted |
| [0003](0003-controller-side-rendering.md) | Render edge configuration on the controller, once per release | Accepted |
| [0004](0004-per-edge-rollout-phases.md) | Pace rollouts from the controller, per edge, in phases | Accepted |
| [0005](0005-origin-hop.md) | Rewrite origin responses on a loopback hop before the cache reads them | Accepted |
| [0006](0006-cache-key-contents.md) | Put the listener port and the encoding in the cache key | Accepted |
| [0007](0007-composed-site-policy.md) | Compose the site policy from feature contributions | Accepted |

## Adding a decision

Copy the shape of an existing page, take the next number, and add a row
above. Write a page when a choice constrains code in more than one place or
would be expensive to reverse; a choice local to one function belongs in its
docstring. When a decision is replaced, keep the page, set its status to
"Superseded by NNNN", and link the new page from it.
