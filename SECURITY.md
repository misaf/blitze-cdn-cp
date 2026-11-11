# Security policy

## Supported versions

Security fixes are made on `master` and shipped in the next release. Only the
latest release receives them; older releases are not patched, so upgrade to the
latest release to pick up a fix.

| Version | Supported |
| --- | --- |
| Latest release (currently 6.1.x) | Yes |
| Earlier releases | No |

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
