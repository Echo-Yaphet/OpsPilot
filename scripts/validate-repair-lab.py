#!/usr/bin/env python3
"""Exercise package validation, binding, one-use approval and rollback boundaries."""

import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


BASE_URL = os.getenv("REPAIR_SANDBOX_URL", "http://repair-sandbox:8095").rstrip("/")
TOKEN = os.getenv("REPAIR_SANDBOX_TOKEN", "opspilot-local-sandbox-token")
APPROVAL_KEY = os.getenv("REPAIR_APPROVAL_KEY", "opspilot-local-approval-key")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
FIXED_CONFIG = {
    "service_version": "payment-lab-v1",
    "redis_url": "redis://repair-lab-redis:6379/0",
}


def call(method, path, payload=None, headers=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(BASE_URL + path, data=data, method=method, headers=headers or HEADERS)
    try:
        with urlopen(request, timeout=8) as response:
            status, body = response.status, json.load(response)
    except HTTPError as exc:
        status = exc.code
        body = json.load(exc)
    assert status == expected, (path, status, body)
    return body


def approval(package, **changes):
    body = {
        "package_id": package["package_id"],
        "package_digest": package["package_digest"],
        "base_digest": package["base_digest"],
        "target": package["target"],
        "expires_at": int(time.time()) + 30,
        "jti": str(uuid4()),
    }
    body.update(changes)
    body["signature"] = hmac.new(
        APPROVAL_KEY.encode(),
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return body


def candidate(base_digest):
    return call("POST", "/v1/candidates", {
        "base_digest": base_digest, "config": FIXED_CONFIG,
    })


call("GET", "/v1/workspace", headers={"Content-Type": "application/json"}, expected=401)
call("POST", "/v1/reset")
workspace = call("GET", "/v1/workspace")
assert workspace["config"]["redis_url"] == "redis://redis.invalid:6379/0"
assert call("GET", "/v1/replica-health")["status_code"] == 503
call("POST", "/v1/shell", {"command": "cat /etc/shadow"}, expected=403)
script = call("POST", "/v1/scripts", {
    "name": "redis-connectivity-check",
    "steps": ["show-config-digest", "resolve-configured-redis"],
})
script_result = call("POST", f"/v1/scripts/{script['script_id']}/run", {})
assert [item["command"] for item in script_result["results"]] == script["steps"]
call("POST", "/v1/candidates", {
    "base_digest": workspace["base_digest"],
    "config": {**FIXED_CONFIG, "redis_url": "redis://evil:6379/0"},
}, expected=422)

package = candidate(workspace["base_digest"])
bad_signature = approval(package)
bad_signature["signature"] = "0" * 64
call("POST", f"/v1/packages/{package['package_id']}/apply", bad_signature, expected=401)
signed = approval(package)
result = call("POST", f"/v1/packages/{package['package_id']}/apply", signed)
assert result["verified"] is True
assert call("GET", "/v1/replica-health")["status_code"] == 200
call("POST", f"/v1/packages/{package['package_id']}/apply", signed, expected=401)

workspace = call("POST", "/v1/reset")
package_a = candidate(workspace["base_digest"])
package_b = candidate(workspace["base_digest"])
tampered = approval(package_a, package_digest="sha256:" + "0" * 64)
call("POST", f"/v1/packages/{package_a['package_id']}/apply", tampered, expected=409)
call("POST", f"/v1/packages/{package_b['package_id']}/apply", approval(package_b))
call("POST", f"/v1/packages/{package_a['package_id']}/apply", approval(package_a), expected=409)

print(json.dumps({
    "status": "passed", "initial_fault": 503, "verified_repair": 200,
    "unauthenticated": 401, "unlisted_shell": 403, "invalid_candidate": 422,
    "bad_signature": 401, "approval_replay": 401, "tampered_or_stale": 409,
    "generated_script_steps": len(script["steps"]),
}, sort_keys=True))
