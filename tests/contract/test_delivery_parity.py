"""Which operations each delivery surface carries, declared rather than inferred.

`test_frozen` pins the CLI and the HTTP API separately, so a route added
without its command (or the reverse) passes there. The surfaces need not agree
everywhere (`config set` edits a local `.env`, `GET /metrics` answers a
scrape), but each asymmetry must be a recorded decision.

The map below is exhaustive by construction: an unclassified command or route
fails the tests underneath with the entry it needs.

Pairing is by operation, not by shape: many `domain` commands map to one
`PATCH /v3/domains/{domain}` (see `blitzecdn.capabilities.dns.cli`).
"""

from __future__ import annotations

import support.frozen_surfaces as surfaces

#: One row per operation: the owner, the command, the route, and for a one-sided
#: row, why the other surface does not carry it. `None` on the CLI side claims
#: the operation is not one an operator types.
_DELIVERY: tuple[tuple[str, str | None, str | None, str], ...] = (
    # -- Zones: policy edits, all landing on the one patch route -----------
    ("blitzecdn", "domain add", "POST /v3/domains", ""),
    ("blitzecdn", "domain list", "GET /v3/domains", ""),
    ("blitzecdn", "domain show", "GET /v3/domains/{domain}", ""),
    ("blitzecdn", "domain remove", "DELETE /v3/domains/{domain}", ""),
    ("blitzecdn", "domain enable", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain origin", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain ssl", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain ssl-automatic", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain minimum-tls", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain always-use-https", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain http3", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain max-upload-size", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain cache", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain cache-query-string", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain compression", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain firewall", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain under-attack", "PATCH /v3/domains/{domain}", ""),
    ("blitzecdn", "domain visitor-headers", "PATCH /v3/domains/{domain}", ""),
    # -- Derived hosts: read-only on both sides ----------------------------
    ("blitzecdn", "domain hosts", "GET /v3/hosts", ""),
    (
        "blitzecdn",
        None,
        "GET /v3/hosts/{name}",
        "one derived host by name. `domain hosts` prints the whole set and an "
        "operator greps it; a client fetching one host by name would rather "
        "not receive the fleet.",
    ),
    (
        "blitzecdn",
        None,
        "GET /v3/domains/{domain}/resolve",
        "what a hostname resolves to once the rules are applied — a preview a "
        "UI shows beside the rule being edited. The CLI's answer to the same "
        "question is `rule show` plus `domain hosts`.",
    ),
    # -- Records -----------------------------------------------------------
    ("blitzecdn", "record add", "POST /v3/domains/{domain}/records", ""),
    ("blitzecdn", "record list", "GET /v3/domains/{domain}/records", ""),
    ("blitzecdn", "record proxy", "PATCH /v3/domains/{domain}/records/{name}", ""),
    ("blitzecdn", "record unproxy", "PATCH /v3/domains/{domain}/records/{name}", ""),
    ("blitzecdn", "record remove", "DELETE /v3/domains/{domain}/records/{name}", ""),
    ("blitzecdn", "dns export", "GET /v3/dns/export", ""),
    # -- Rules -------------------------------------------------------------
    ("blitzecdn", "rule add", "POST /v3/domains/{domain}/rules", ""),
    ("blitzecdn", "rule list", "GET /v3/domains/{domain}/rules", ""),
    ("blitzecdn", "rule show", "GET /v3/domains/{domain}/rules/{name}", ""),
    ("blitzecdn", "rule set", "PATCH /v3/domains/{domain}/rules/{name}", ""),
    ("blitzecdn", "rule remove", "DELETE /v3/domains/{domain}/rules/{name}", ""),
    # -- Edges -------------------------------------------------------------
    ("blitzecdn", "edge add", "POST /v3/edges", ""),
    ("blitzecdn", "edge list", "GET /v3/edges", ""),
    ("blitzecdn", "edge update", "PATCH /v3/edges/{name}", ""),
    ("blitzecdn", "edge remove", "DELETE /v3/edges/{name}", ""),
    (
        "blitzecdn",
        None,
        "GET /v3/edges/{name}",
        "one edge by name; see GET /v3/hosts/{name}.",
    ),
    (
        "blitzecdn",
        "edge image spec",
        None,
        "the pinned edge image and its digest, read out of this checkout's own "
        "version pins. It describes the installed control plane rather than "
        "any state it holds, which is the same reason `features` is here.",
    ),
    # -- Traffic -----------------------------------------------------------
    ("blitzecdn", "traffic usage", "GET /v3/traffic/usage", ""),
    ("blitzecdn", "traffic status", "GET /v3/traffic/status/{edge}", ""),
    # -- Durable work ------------------------------------------------------
    (
        "blitzecdn",
        "job list",
        None,
        "what background work is queued, running or failed. Deliberately not a "
        "route: a job is control-plane machinery rather than a resource a "
        "client manages, and the two things a client actually wants — did my "
        "deployment run, did my certificate renew — are already answered by "
        "`GET /v3/deployments/{deployment_id}` and `GET /v3/workflows`. "
        "Publishing the queue would be publishing an implementation.",
    ),
    ("blitzecdn", "job show", None, "one job, and the error that ended it; see above."),
    (
        "blitzecdn",
        "job schedules",
        None,
        "when each recurring job is next due. Same reasoning: the schedule is "
        "how the control plane paces itself, not a resource, and a client that "
        "wants a certificate renewed now asks for that rather than waiting on "
        "a timer.",
    ),
    # -- Releases ----------------------------------------------------------
    ("blitzecdn", "release list", "GET /v3/releases", ""),
    ("blitzecdn", "release show", "GET /v3/releases/{release_id}", ""),
    ("blitzecdn", "release show", "GET /v3/releases/current", ""),
    (
        "blitzecdn",
        "release artifact",
        "GET /v3/releases/{release_id}/artifacts/{name}",
        "",
    ),
    (
        "blitzecdn",
        "release render",
        None,
        "writes the rendered edge configuration tree, with its links and seal "
        "manifest, into a local directory for review. A tree of files and "
        "symlinks is a filesystem artifact rather than a resource; an API "
        "client reads the same release through its artifact route.",
    ),
    (
        "blitzecdn",
        "release explain",
        None,
        "where each effective setting came from. The route answers it too — an "
        "explanation is part of every release document — so a client asking "
        "`GET /v3/releases/{release_id}` already has it and needs no endpoint "
        "of its own. The command exists because a terminal wants one host's "
        "settings rather than the fleet's.",
    ),
    # -- Deployments -------------------------------------------------------
    ("blitzecdn", "deploy", "POST /v3/deployments", ""),
    ("blitzecdn", "status", "GET /v3/deployments", ""),
    ("blitzecdn", "status", "GET /v3/deployments/{deployment_id}", ""),
    (
        "blitzecdn",
        "status",
        "GET /v3/deployments/{deployment_id}/targets",
        "how far each edge got. `blitzecdn status <id>` prints it beneath the "
        "deployment, because an operator asking about a run wants the per-edge "
        "answer in the same breath; a client polling a rollout wants to fetch "
        "just the progress without re-reading the deployment each time.",
    ),
    ("blitzecdn", "rollback", "POST /v3/rollbacks", ""),
    ("blitzecdn", "drift", "POST /v3/drift", ""),
    (
        "blitzecdn",
        None,
        "GET /v3/deployments/{deployment_id}/drift",
        "the drift a past deployment recorded. `blitzecdn drift` measures it "
        "now, which is the question at a terminal; reading an old deployment's "
        "answer is the question a dashboard asks.",
    ),
    ("blitzecdn", "audit", "GET /v3/audit-events", ""),
    (
        "blitzecdn",
        None,
        "GET /v3/workflows",
        "progress of an operation still running. A command *is* the operation "
        "and prints as it goes, so it has nothing to poll; an HTTP client that "
        "started one has only this.",
    ),
    ("blitzecdn", None, "GET /v3/workflows/{workflow_id}", "see GET /v3/workflows."),
    (
        "blitzecdn",
        "plan",
        None,
        "what a deploy would do, without doing it. Reads the same state "
        "`deploy` would and writes nothing, so an HTTP caller can have the "
        "answer by not calling POST /v3/deployments.",
    ),
    # -- Diagnostics: about this install, not about the fleet --------------
    ("blitzecdn", "overview", "GET /v3/status", ""),
    (
        "blitzecdn",
        None,
        "GET /health",
        "a readiness probe, for whatever restarts this process. Nobody types "
        "it; `doctor` is the question an operator asks.",
    ),
    ("blitzecdn", None, "GET /metrics", "a Prometheus scrape target."),
    (
        "blitzecdn",
        "doctor",
        None,
        "checks the host this runs on — the database, the Ansible tree, the "
        "SSH keys, the resolver. Every answer is about the machine the command "
        "is typed on, and served over HTTP it would describe a machine the "
        "caller is not on.",
    ),
    (
        "blitzecdn",
        "validate",
        None,
        "refuses a deploy that would render a broken vhost. Reachable over "
        "HTTP only as the deploy it precedes, which runs it.",
    ),
    (
        "blitzecdn",
        "features",
        None,
        "which features this controller ships. Describes the control plane "
        "itself; see `edge image spec`.",
    ),
    (
        "blitzecdn",
        "ansible roles-path",
        None,
        "the resolved role search path on this host.",
    ),
    ("blitzecdn", "ansible slots", None, "see `ansible roles-path`."),
    # -- The install itself ------------------------------------------------
    (
        "blitzecdn",
        "init",
        None,
        "writes the `.env` this control plane reads. It runs before there is a "
        "control plane to serve anything, which is what keeps all four of "
        "these off the API.",
    ),
    ("blitzecdn", "setup", None, "creates local schema and credentials; see `init`."),
    ("blitzecdn", "migrate", None, "offline schema maintenance before services start."),
    ("blitzecdn", "serve", None, "starts the API. It cannot be one of its own routes."),
    (
        "blitzecdn",
        "config list",
        None,
        "reads and writes the `.env`; see `init`. Exposing it would also let "
        "an API caller change the credentials the API authenticates with.",
    ),
    ("blitzecdn", "config set", None, "see `config list`."),
    ("blitzecdn", "config unset", None, "see `config list`."),
    # -- Edge operations ---------------------------------------------------
    (
        "blitzecdn",
        None,
        "GET /v3/operations/{operation_id}",
        "polls an edge operation the API queued for the worker. The CLI runs "
        "the same operations in-process and prints their result, so it has "
        "nothing to poll.",
    ),
    ("blitzecdn", "cache purge", "POST /v3/cache/purge", ""),
    ("blitzecdn", "stats", "POST /v3/cache/stats", ""),
    ("blitzecdn", "origin check", "POST /v3/origins/check", ""),
    (
        "blitzecdn",
        "cert acme-hook",
        None,
        "Certbot's HTTP-01 hook on this controller; no client asks for it.",
    ),
    (
        "blitzecdn",
        "cert collect",
        None,
        "Controller-local material maintenance; no remote operation is needed.",
    ),
    ("blitzecdn", "cert list", "GET /v3/certificates", ""),
    (
        "blitzecdn",
        "cert preflight",
        "GET /v3/hosts/{name}/certificate/preflight",
        "",
    ),
    ("blitzecdn", "cert renew", "POST /v3/certificates/renew", ""),
    ("blitzecdn", "cert reconcile", "POST /v3/certificates/reconcile", ""),
    (
        "blitzecdn",
        "cert upload",
        "POST /v3/hosts/{name}/certificate/upload",
        "",
    ),
    (
        "blitzecdn",
        "cert request",
        "POST /v3/hosts/{name}/certificate/request",
        "",
    ),
    ("blitzecdn", "ssl reconcile", "POST /v3/ssl/automatic/reconcile", ""),
    (
        "blitzecdn",
        None,
        "GET /v3/hosts/{name}/certificate",
        "one host's certificate. `cert list` prints the fleet's, sorted by "
        "expiry, which is the form a human wants; see GET /v3/hosts/{name}.",
    ),
    (
        "blitzecdn",
        "backup create",
        None,
        "reads and writes archives on the control-plane host, including the "
        "ACME account key. Over HTTP it would be an endpoint that hands the "
        "caller every private key the control plane holds.",
    ),
    ("blitzecdn", "backup inspect", None, "see `backup create`."),
    (
        "blitzecdn",
        "backup prune",
        None,
        "deletes archives on the control-plane host; the host timer runs it "
        "after each scheduled `backup create`. See `backup create`.",
    ),
    ("blitzecdn", "backup restore", None, "see `backup create`."),
    (
        "blitzecdn",
        "backup recover",
        None,
        "runs with the controller stopped, the API among it, and puts back "
        "the state an unfinished restore replaced. No request could reach it.",
    ),
    (
        "blitzecdn",
        "backup commit-restore",
        None,
        "hidden; the host wrapper's acknowledgement that restored "
        "configuration was published, run with the controller stopped. See "
        "`backup recover`.",
    ),
)


def _declared(index: int) -> set[str]:
    """Every declared operation on one side of the application boundary."""
    return {row[index] for row in _DELIVERY if row[index] is not None}


def _live_commands() -> set[str]:
    return {
        line.split("\t")[2].split("\t")[0].removeprefix("blitzecdn ")
        for line in surfaces.cli_surface().splitlines()
    }


def _live_routes(settings) -> set[str]:
    return {
        line.split("\t")[2]
        for line in surfaces.http_surface(settings).splitlines()
        if line.split("\t")[1] == "route"
    }


def _report(kind: str, unclassified: set[str], stale: set[str]) -> str:
    problems = []
    if unclassified:
        problems.append(
            f"{kind} present in this tree but not in _DELIVERY: "
            + ", ".join(sorted(unclassified))
            + ". Add a row saying whether the other surface should carry this "
            "operation too — and if it should not, why."
        )
    if stale:
        problems.append(
            f"_DELIVERY names {kind} that no longer exist: "
            + ", ".join(sorted(stale))
            + ". Remove the rows, or the map is describing a surface this tree "
            "does not have."
        )
    return "\n\n".join(problems)


def test_every_command_is_classified():
    """No command without a row, and no row without a command."""
    live = _live_commands()
    declared = _declared(1)
    report = _report("commands", live - declared, declared - live)
    assert not report, report


def test_every_route_is_classified(settings):
    """No route without a row, and no row without a route."""
    live = _live_routes(settings)
    declared = _declared(2)
    report = _report("routes", live - declared, declared - live)
    assert not report, report


def test_a_one_sided_operation_says_why():
    """A row naming one surface has to make the case; a paired row need not."""
    silent = sorted(
        row[1] or row[2] or "?"
        for row in _DELIVERY
        if (row[1] is None) != (row[2] is None) and not row[3].strip()
    )
    assert not silent, (
        "these operations are on one surface only and say nothing about why: "
        + ", ".join(silent)
    )


def test_every_failure_kind_reaches_both_surfaces():
    """Neither delivery layer may leave a `FailureKind` unanswered.

    Core owns the taxonomy and each layer only its projection to an exit code or
    HTTP status. Both tables are subscripted, so an uncovered kind would raise a
    `KeyError` while a failure is being reported.
    """
    from blitzecdn.core.exceptions import FailureKind
    from blitzecdn.entrypoints.api.app import _HTTP_STATUS
    from blitzecdn.entrypoints.cli.main import _EXIT_CODES

    missing = {
        "exit codes (blitzecdn.entrypoints.cli.main._EXIT_CODES)": set(FailureKind)
        - set(_EXIT_CODES),
        "HTTP statuses (blitzecdn.entrypoints.api.app._HTTP_STATUS)": set(FailureKind)
        - set(_HTTP_STATUS),
    }
    report = "; ".join(
        f"{table} does not answer {sorted(k.value for k in kinds)}"
        for table, kinds in missing.items()
        if kinds
    )
    assert not report, report


def test_every_error_class_is_classified():
    """Every `BlitzeError` subclass names a kind deliberately.

    `classify` falls back to INTERNAL, which tells a caller the installation is
    broken; that is wrong for a conflict or a missing thing, so a new subclass must
    say which kind it is.
    """
    from blitzecdn.core import exceptions
    from blitzecdn.core.exceptions import BlitzeError, FeatureError

    classification = exceptions._CLASSIFICATION
    classified = {kind for kind, _ in classification}
    subclasses = {
        value
        for value in vars(exceptions).values()
        if isinstance(value, type)
        and issubclass(value, BlitzeError)
        and value is not BlitzeError
    }
    unclassified = sorted(cls.__name__ for cls in subclasses - classified)
    assert not unclassified, (
        "these BlitzeError subclasses fall through to the INTERNAL default: "
        + ", ".join(unclassified)
        + ". Add each to _CLASSIFICATION with the kind it actually is."
    )
    # A subclass listed below its base can never be reached.
    for position, (kind, _) in enumerate(classification):
        for later, _ in classification[position + 1 :]:
            assert not issubclass(later, kind) or later is kind, (
                f"{kind.__name__} is a base of {later.__name__} but is listed "
                "before it, so the subclass can never match"
            )
    assert exceptions.classify(FeatureError("x")) is exceptions.FailureKind.INTERNAL
