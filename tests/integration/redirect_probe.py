"""Request the harness's redirect hosts as a visitor and check what they answer.

Run by `tests/integration/http3-edge-integration.sh` in a throwaway Python
container on the visitors' network, with the edge's address as the only
argument. The hosts come from `tests/integration/http3-edge/redirects.yml`.
Standard library only: the container has nothing else, and `python -I` keeps
the read-only checkout off the import path.

Every response is printed as one JSON line (host, request, status, headers), so
a failed run's log shows what the edge actually answered. Each check that fails
is reported, and the exit status is 1 if any did.

One process makes every request, so Under Attack Mode's binding — the host, the
client address and the User-Agent — is the same for the challenge, its proof
and the request that carries the clearance.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import time
from urllib.parse import parse_qs, urlsplit

USER_AGENT = "blitzecdn-redirect-probe/1"
#: blitzecdn_security_under_attack_difficulty_bits caps at 20; a proof is
#: found long before this many tries at any difficulty the role accepts.
PROOF_LIMIT = 1 << 26

failures: list[str] = []


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def request(
    edge: str,
    host: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    connection = http.client.HTTPConnection(edge, 80, timeout=15)
    sent = {"Host": host, "User-Agent": USER_AGENT, **(headers or {})}
    try:
        connection.request(method, path, body=body, headers=sent)
        answer = connection.getresponse()
        payload = answer.read()
        received = {name.lower(): value for name, value in answer.getheaders()}
        response = Response(answer.status, received, payload)
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "host": host,
                "request": f"{method} {path}",
                "status": response.status,
                "headers": response.headers,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return response


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr, flush=True)
        failures.append(message)


def redirected(response: Response, status: int, location: str, what: str) -> None:
    check(
        response.status == status and response.header("location") == location,
        f"{what}: expected {status} to {location!r}, got {response.status} "
        f"to {response.header('location')!r}",
    )


def refused(response: Response, status: int, what: str) -> None:
    check(
        response.status == status,
        f"{what}: expected {status}, got {response.status}",
    )
    check(
        response.header("location") is None,
        f"{what}: a refused request still carried Location "
        f"{response.header('location')!r}",
    )


def suffix_and_query(edge: str) -> None:
    host = "www.redirect-plain.test"
    redirected(
        request(edge, host, "/old/a%20b/c?x=1&y=2"),
        301,
        "/new/a%20b/c?x=1&y=2",
        "the suffix, still percent-encoded, and the query string",
    )
    redirected(request(edge, host, "/old/"), 301, "/new/", "an empty suffix")
    redirected(
        request(edge, host, "/gone.html?drop=1"),
        302,
        "/",
        "a rule that drops the query string",
    )


def request_rules(edge: str) -> None:
    host = "www.redirect-plain.test"
    refused(
        request(edge, host, "/old/a", method="DELETE"),
        405,
        "a denied method on a redirected path",
    )
    refused(
        request(edge, host, "/retired/a"),
        403,
        "a redirected path outside the allowed paths",
    )


def client_rules(edge: str) -> None:
    refused(
        request(edge, "www.redirect-country.test", "/old/a"),
        403,
        "a visitor outside the allowed countries",
    )
    refused(
        request(edge, "www.redirect-sources.test", "/old/a"),
        403,
        "a visitor from a denied source",
    )


def rate_limit(edge: str) -> None:
    host = "www.redirect-rate.test"
    redirected(request(edge, host, "/old/a"), 301, "/new/a", "the first request")
    refused(
        request(edge, host, "/old/a"),
        429,
        "a second request inside the rate's interval",
    )


def maintenance(edge: str) -> None:
    response = request(edge, "www.redirect-maintenance.test", "/old/a")
    refused(response, 503, "a redirected path under maintenance")
    check(
        response.header("retry-after") == "120",
        f"maintenance answered Retry-After {response.header('retry-after')!r}",
    )


def solve(token: str, bits: int) -> int:
    for proof in range(PROOF_LIMIT):
        digest = hashlib.sha256(f"{token}:{proof}".encode()).digest()
        if int.from_bytes(digest, "big") >> (256 - bits) == 0:
            return proof
    raise RuntimeError("no proof found")


def difficulty(page: bytes) -> int:
    """The bits the challenge page's own script tests for."""
    marker = b"if(meets(hash,"
    start = page.index(marker) + len(marker)
    return int(page[start : page.index(b")", start)])


def under_attack(edge: str) -> None:
    host = "www.redirect-attack.test"
    target = "/old/a?x=1"
    challenge = request(edge, host, target)
    location = challenge.header("location") or ""
    challenged = (
        challenge.status == 302
        and challenge.header("x-blitzecdn-mitigation") == "challenge"
        and location.startswith("/.blitzecdn/challenge?token=")
    )
    check(
        challenged,
        "a redirected path was not challenged first: "
        f"{challenge.status} to {location!r}",
    )
    if not challenged:
        return

    token = parse_qs(urlsplit(location).query)["token"][0]
    page = request(edge, host, location)
    check(page.status == 200, f"the challenge page answered {page.status}")
    if page.status != 200:
        return
    started = time.monotonic()
    proof = solve(token, difficulty(page.body))
    print(f"solved the challenge in {time.monotonic() - started:.1f}s", flush=True)

    verdict = request(
        edge,
        host,
        "/.blitzecdn/challenge/verify",
        method="POST",
        body=json.dumps({"token": token, "proof": proof}).encode(),
        headers={"Content-Type": "application/json"},
    )
    cookie = (verdict.header("set-cookie") or "").split(";", 1)[0]
    check(
        verdict.status == 200 and cookie.startswith("blitzecdn_clearance="),
        f"the proof was not accepted: {verdict.status}",
    )
    if verdict.status != 200:
        return
    check(
        json.loads(verdict.body).get("redirect") == target,
        f"the verifier sent the visitor to {verdict.body!r}, not {target!r}",
    )
    redirected(
        request(edge, host, target, headers={"Cookie": cookie}),
        301,
        "/new/a?x=1",
        "a cleared visitor",
    )


def main(edge: str) -> int:
    for scenario in (
        suffix_and_query,
        request_rules,
        client_rules,
        rate_limit,
        maintenance,
        under_attack,
    ):
        print(f"--- {scenario.__name__}", flush=True)
        scenario(edge)
    if failures:
        print(f"{len(failures)} redirect check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
