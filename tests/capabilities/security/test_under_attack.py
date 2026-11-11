"""Execute the rendered edge challenge module against hostile token inputs.

The template is read from the same path a release is rendered from. It
carries no secret: the key arrives per request as `$blitzecdn_under_attack_key`,
which this harness sets the way the edge-local include does.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import jinja2

from blitzecdn.capabilities.security.feature import NGINX_TEMPLATES

TEMPLATES = NGINX_TEMPLATES
SECRET = "edge-test-secret-" + "x" * 32


def _render_module(difficulty_bits: int = 16) -> str:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        undefined=jinja2.StrictUndefined,
        autoescape=jinja2.select_autoescape(disabled_extensions=("j2",)),
    )
    environment.filters["to_json"] = json.dumps
    return environment.get_template("under-attack.js.j2").render(
        blitzecdn_security_under_attack_passage_seconds=1800,
        blitzecdn_security_under_attack_difficulty_bits=difficulty_bits,
    )


def test_signed_challenges_proofs_clearance_and_output_escaping(tmp_path: Path):
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise the rendered njs module"
    module = tmp_path / "under-attack.mjs"
    module.write_text(_render_module(), encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        """
import crypto from 'crypto';
import mitigation from './under-attack.mjs';

let clock = 1700000000;
Date.now = () => clock * 1000;

function request(options = {}) {
    const r = {
        method: options.method || 'GET',
        remoteAddress: options.address || '2001:db8::10',
        headersIn: {'User-Agent': options.agent || 'Test Browser/1'},
        headersOut: {},
        variables: {
            host: 'example.com',
            scheme: options.scheme || 'https',
            blitzecdn_under_attack_target: options.target || '/account?tab=2',
            cookie_blitzecdn_clearance: options.cookie || '',
            request_id: options.requestId || '0123456789abcdef0123456789abcdef',
            blitzecdn_under_attack_key: 'key' in options ? options.key : '__KEY__',
        },
        args: {token: options.token || ''},
        requestText: options.body,
        return(status, body) { this.status = status; this.body = body; },
    };
    return r;
}

function issue(target = '/account?tab=2') {
    const r = request({target});
    mitigation.guard(r);
    const location = r.variables.blitzecdn_under_attack_location;
    return {r, token: location.split('token=')[1]};
}

function proofFor(token) {
    for (let proof = 0; proof <= 0xffffffff; proof += 1) {
        const hash = crypto.createHash('sha256').update(token + ':' + proof).digest();
        if (hash[0] === 0 && hash[1] === 0) return proof;
    }
    throw new Error('proof space exhausted');
}

const issued = issue();
const proof = proofFor(issued.token);
const badProof = proof === 0 ? 1 : 0;

const invalidProof = request({
    method: 'POST',
    body: JSON.stringify({token: issued.token, proof: badProof}),
});
mitigation.verify(invalidProof);

const verified = request({
    method: 'POST',
    body: JSON.stringify({token: issued.token, proof}),
});
mitigation.verify(verified);
const cookieHeader = verified.headersOut['Set-Cookie'];
const clearance = cookieHeader.match(/^blitzecdn_clearance=([^;]+)/)[1];

const cleared = request({cookie: clearance});
mitigation.guard(cleared);
const otherIp = request({cookie: clearance, address: '2001:db8::11'});
mitigation.guard(otherIp);
const tamperedClearance = request({
    cookie: clearance.slice(0, -1) + (clearance.endsWith('A') ? 'B' : 'A'),
});
mitigation.guard(tamperedClearance);

const tamperedChallenge = request({
    token: issued.token.slice(0, -1) + (issued.token.endsWith('A') ? 'B' : 'A'),
});
mitigation.challenge(tamperedChallenge);

const expiring = issue().token;
clock += 331;
const expiredChallenge = request({token: expiring});
mitigation.challenge(expiredChallenge);
clock += 1800;
const expiredClearance = request({cookie: clearance});
mitigation.guard(expiredClearance);

clock = 1700000000;
const hostileTarget = '/<script>alert(1)</script>';
const hostile = issue(hostileTarget);
const page = request({token: hostile.token});
mitigation.challenge(page);
const unsafeAbsolute = request({target: '//evil.example/path'});
mitigation.guard(unsafeAbsolute);
const unsafeControl = request({target: '/ok\\r\\nX-Evil: yes'});
mitigation.guard(unsafeControl);

let unkeyed = 'served';
try {
    mitigation.guard(request({key: ''}));
} catch (error) {
    unkeyed = error.message;
}

console.log(JSON.stringify({
    unkeyed,
    challengeStatus: issued.r.status,
    mitigationHeader: issued.r.headersOut['X-BlitzeCDN-Mitigation'],
    invalidProof: invalidProof.status,
    verified: verified.status,
    secureCookie: cookieHeader.includes('; Secure'),
    httpOnlyCookie: cookieHeader.includes('; HttpOnly'),
    sameSiteCookie: cookieHeader.includes('; SameSite=Lax'),
    cleared: cleared.status,
    otherIp: otherIp.status,
    tamperedClearance: tamperedClearance.status,
    tamperedChallenge: tamperedChallenge.status,
    expiredChallenge: expiredChallenge.status,
    expiredClearance: expiredClearance.status,
    unsafeAbsolute: unsafeAbsolute.status,
    unsafeControl: unsafeControl.status,
    pageStatus: page.status,
    pageContainsHostileTarget: page.body.includes(hostileTarget),
    csp: page.headersOut['Content-Security-Policy'],
}));
""".replace("__KEY__", base64.b64encode(SECRET.encode()).decode()),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 -- node and both paths are resolved locally
        [node, str(harness)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

    # Without the edge-local key the module refuses rather than signing with
    # an empty secret anyone could reproduce.
    assert report["unkeyed"] == "the Under Attack Mode signing key is not configured"
    assert report["challengeStatus"] == 401
    assert report["mitigationHeader"] == "challenge"
    assert report["invalidProof"] == 403
    assert report["verified"] == 200
    assert report["secureCookie"] is True
    assert report["httpOnlyCookie"] is True
    assert report["sameSiteCookie"] is True
    assert report["cleared"] == 204
    assert report["otherIp"] == 401
    assert report["tamperedClearance"] == 401
    assert report["tamperedChallenge"] == 400
    assert report["expiredChallenge"] == 400
    assert report["expiredClearance"] == 401
    assert report["unsafeAbsolute"] == 400
    assert report["unsafeControl"] == 400
    assert report["pageStatus"] == 200
    assert report["pageContainsHostileTarget"] is False
    assert "default-src 'none'" in report["csp"]


def _run_harness(tmp_path: Path, module_source: str, body: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise the rendered njs module"
    (tmp_path / "under-attack.mjs").write_text(module_source, encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        body.replace("__KEY__", base64.b64encode(SECRET.encode()).decode()),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 -- node and both paths are resolved locally
        [node, str(harness)], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_difficulty_is_counted_in_bits_by_the_edge_and_the_page(tmp_path: Path):
    """A 12-bit edge takes a proof a 16-bit check would refuse, and only that.

    Twelve is deliberately not a whole number of bytes, so the half-byte
    comparison is what decides. The page carries its own copy of the test, which
    is run here against the edge's verdicts on the same hashes.
    """
    report = _run_harness(
        tmp_path,
        _render_module(difficulty_bits=12),
        """
import crypto from 'crypto';
import mitigation from './under-attack.mjs';

Date.now = () => 1700000000 * 1000;

function request(options = {}) {
    return {
        method: options.method || 'GET',
        remoteAddress: '2001:db8::10',
        headersIn: {'User-Agent': 'Test Browser/1'},
        headersOut: {},
        variables: {
            host: 'example.com',
            scheme: 'https',
            blitzecdn_under_attack_target: '/',
            cookie_blitzecdn_clearance: '',
            request_id: '0123456789abcdef0123456789abcdef',
            blitzecdn_under_attack_key: '__KEY__',
        },
        args: {token: options.token || ''},
        requestText: options.body,
        return(status, body) { this.status = status; this.body = body; },
    };
}

const guarded = request();
mitigation.guard(guarded);
const token = guarded.variables.blitzecdn_under_attack_location.split('token=')[1];

function find(test) {
    for (let proof = 0; proof <= 0xffffffff; proof += 1) {
        const hash = crypto.createHash('sha256').update(token + ':' + proof).digest();
        if (test(hash)) return proof;
    }
    throw new Error('proof space exhausted');
}

function submit(proof) {
    const r = request({method: 'POST', body: JSON.stringify({token, proof})});
    mitigation.verify(r);
    return r.status;
}

// Twelve zero bits but not sixteen: only a bitwise check accepts it.
const twelve = find((h) => h[0] === 0 && h[1] !== 0 && (h[1] >> 4) === 0);
// Eight zero bits, then a set bit inside the next four: short by under a byte.
const short = find((h) => h[0] === 0 && (h[1] >> 4) !== 0);

const page = request({token});
mitigation.challenge(page);
const script = page.body.match(/const meets=(.*?);for\\(let proof=/)[1];
const meets = new Function('return ' + script)();
const samples = [
    [[0x00, 0x0f, 0xff], 12, true],
    [[0x00, 0x10, 0x00], 12, false],
    [[0x00, 0x00, 0x01], 16, true],
    [[0x00, 0x00, 0x80], 17, false],
    [[0x01, 0x00, 0x00], 8, false],
    [[0x00, 0x00, 0x00], 24, true],
];
const pageAgrees = samples.every(
    ([bytes, bits, expected]) => meets(Uint8Array.from(bytes), bits) === expected
);

console.log(JSON.stringify({
    twelve: submit(twelve),
    short: submit(short),
    pageDifficulty: page.body.includes('if(meets(hash,12))'),
    pageAgrees,
}));
""",
    )

    assert report["twelve"] == 200
    assert report["short"] == 403
    assert report["pageDifficulty"] is True
    assert report["pageAgrees"] is True


def test_the_challenge_page_nonce_differs_on_every_response(tmp_path: Path):
    """The CSP nonce is per response, not fixed per host, address and User-Agent.

    Pages served to the same client differ in their nonce whenever the request
    id or the clock does, and the script carries the nonce the header names.
    """
    report = _run_harness(
        tmp_path,
        _render_module(),
        """
import mitigation from './under-attack.mjs';

let clock = 1700000000000;
Date.now = () => clock;

function request(requestId, token) {
    return {
        method: 'GET',
        remoteAddress: '2001:db8::10',
        headersIn: {'User-Agent': 'Test Browser/1'},
        headersOut: {},
        variables: {
            host: 'example.com',
            scheme: 'https',
            blitzecdn_under_attack_target: '/',
            cookie_blitzecdn_clearance: '',
            request_id: requestId,
            blitzecdn_under_attack_key: '__KEY__',
        },
        args: {token: token || ''},
        return(status, body) { this.status = status; this.body = body; },
    };
}

const guarded = request('0123456789abcdef0123456789abcdef');
mitigation.guard(guarded);
const token = guarded.variables.blitzecdn_under_attack_location.split('token=')[1];

function page(requestId) {
    const r = request(requestId, token);
    mitigation.challenge(r);
    const header = r.headersOut['Content-Security-Policy'].match(/'nonce-([^']+)'/)[1];
    const script = r.body.match(/<script nonce="([^"]+)">/)[1];
    return {status: r.status, header, script};
}

const first = page('11111111111111111111111111111111');
const otherRequest = page('22222222222222222222222222222222');
clock += 1;
const later = page('11111111111111111111111111111111');

console.log(JSON.stringify({first, otherRequest, later}));
""",
    )

    first, other, later = report["first"], report["otherRequest"], report["later"]
    assert {first["status"], other["status"], later["status"]} == {200}
    for served in (first, other, later):
        assert served["header"] == served["script"]
        assert len(served["header"]) == 24
    assert len({first["header"], other["header"], later["header"]}) == 3
