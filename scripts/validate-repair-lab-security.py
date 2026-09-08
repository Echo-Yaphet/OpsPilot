#!/usr/bin/env python3
"""Validate runtime isolation from Docker's effective container configuration."""

import json
import subprocess


containers = {
    "sandbox": "opspilot-repair-sandbox-1",
    "validator": "opspilot-repair-validator-1",
    "replica": "opspilot-repair-lab-payment-1",
    "redis": "opspilot-repair-lab-redis-1",
}
documents = json.loads(subprocess.run(
    ["docker", "inspect", *containers.values()], check=True, capture_output=True, text=True,
).stdout)

for document in documents:
    host = document["HostConfig"]
    assert host["ReadonlyRootfs"] is True
    assert "ALL" in (host["CapDrop"] or [])
    assert "no-new-privileges:true" in (host["SecurityOpt"] or [])
    assert all("docker.sock" not in mount.get("Source", "") for mount in document["Mounts"])

sandbox = documents[0]
assert sandbox["Config"]["User"] == "10001"
assert sandbox["HostConfig"]["PidsLimit"] == 64
assert sandbox["HostConfig"]["Memory"] == 256 * 1024 * 1024
assert sandbox["HostConfig"]["NanoCpus"] == 500_000_000
assert set(sandbox["NetworkSettings"]["Networks"]) == {
    "opspilot_repair-control", "opspilot_repair-runtime",
}
print(json.dumps({"status": "passed", "containers": list(containers), "docker_socket": False}))
