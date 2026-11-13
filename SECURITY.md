# Security policy

## Supported versions

Security fixes are made on `7.x`, the default branch, and shipped in the next
7.y.z release; they are then merged up into `master`, which holds the next
major. Only the latest 7.x release receives them. Earlier 7.x releases are not
patched, so upgrade in place to the latest 7.x release to pick up a fix.

| Version | Supported |
| --- | --- |
| 7.x (the latest 7.y.z release) | Yes |
| 6.x and earlier | No. 7.0.0 is a fresh install with no upgrade path from 6.x, so move to 7.x with a new install |

## Reporting a vulnerability

Do not report vulnerabilities in public issues, pull requests, or discussions.

Report them privately through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**, or
go directly to
<https://github.com/misaf/blitze-cdn-cp/security/advisories/new>.

Include what you can of:

- the affected component (API, worker, CLI, edge, installer, or other) and the
  BlitzeCDN version or commit;
- steps to reproduce or a proof of concept;
- the impact you expect and any preconditions, such as required credentials or
  network position.

Do not include real API keys, credentials, or customer data; use placeholders.

## What to expect

The report stays private between you and the maintainers while it is assessed
and fixed. You will be kept informed in the advisory thread, and a fix is
published as a release together with a GitHub security advisory. Reporters are
credited in the advisory unless they ask not to be.
