"""Execute the rendered edge challenge module against hostile token inputs.

Here rather than in `tests/capabilities/security/` because this is the security
capability's edge behaviour, and this distribution is where that capability
lives. What core keeps — the `under_attack_mode` switch and the firewall rule
contract that a detached controller must still be able to read back — is
asserted beside the contract, in `tests/capabilities/security/test_security_policy.py`.

The template belongs to the `blitzecdn_security` role, which ships inside this
wheel beside the plugin that declares the capability — so the module under test
here is read from the same path a deployment resolves it by.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import jinja2
from blitzecdn_security import ansible

TEMPLATES = ansible.ROLES_PATH / ansible.EDGE_ROLE / "templates"


def _render_module() -> str:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        undefined=jinja2.StrictUndefined,
        autoescape=jinja2.select_autoescape(disabled_extensions=("j2",)),
    )
    environment.filters["to_json"] = json.dumps
    return environment.get_template("under-attack.js.j2").render(
        blitzecdn_security_under_attack_secret="edge-test-secret-" + "x" * 32,
        blitzecdn_security_under_attack_passage_seconds=1800,
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

console.log(JSON.stringify({
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
""",
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 -- node and both paths are resolved locally
        [node, str(harness)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

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
