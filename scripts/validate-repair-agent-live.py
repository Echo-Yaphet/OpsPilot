#!/usr/bin/env python3
"""Run the real local model through proposal, human approval and verification."""

import json
import subprocess
from urllib.request import Request, urlopen


def post(url, payload, timeout):
    request = Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


reset_code = (
    "import urllib.request; "
    "r=urllib.request.Request('http://localhost:8095/v1/reset',data=b'',method='POST',"
    "headers={'Authorization':'Bearer opspilot-local-sandbox-token'}); "
    "urllib.request.urlopen(r,timeout=5).read()"
)
subprocess.run(
    ["docker", "compose", "exec", "-T", "repair-sandbox", "python", "-c", reset_code],
    check=True,
)
proposal = post("http://localhost:8080/api/v1/repair-lab/proposals", {
    "symptom": "payment repair replica is unhealthy because configured Redis DNS cannot resolve",
}, 180)
assert proposal["status"] == "awaiting_approval"
assert proposal["package"]["validation"]["passed"] is True
assert [item["tool"] for item in proposal["trace"]] == [
    "read_workspace", "write_diagnostic_script", "run_diagnostic_script", "write_candidate",
]
applied = post("http://localhost:8080/api/v1/repair-lab/approvals", {
    "package_id": proposal["package"]["package_id"], "approved": True,
}, 30)
assert applied["verified"] is True
print(json.dumps({
    "status": "passed", "model": proposal["model"],
    "elapsed_seconds": proposal["elapsed_seconds"], "usage": proposal["usage"],
    "tools": [item["tool"] for item in proposal["trace"]],
    "package_id": proposal["package"]["package_id"],
    "package_digest": proposal["package"]["package_digest"],
    "target": proposal["package"]["target"], "verified": applied["verified"],
    "approval_jti": applied["approval"]["jti"],
}, ensure_ascii=False, sort_keys=True))
