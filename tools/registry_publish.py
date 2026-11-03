"""Publish server.json to the official MCP Registry with HTTP domain verification.

Same steps as mcp-publisher's `login http` + `publish`: sign the current time with the
domain's Ed25519 key, trade it for a registry token, then publish. Never prints the key
or the token.
"""
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

REGISTRY = "https://registry.modelcontextprotocol.io"
DOMAIN = "dmvtransit.com"
KEY = Path.home() / ".dmvtransit" / "mcp-registry-key.pem"
SERVER_JSON = Path(__file__).resolve().parents[1] / "server.json"

ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
with tempfile.TemporaryDirectory() as d:
    msg = Path(d) / "ts.txt"
    msg.write_bytes(ts.encode())
    sig = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(KEY),
                          "-in", str(msg)], capture_output=True, check=True).stdout
print("signature bytes:", len(sig))

with httpx.Client(timeout=30) as http:
    r = http.post(f"{REGISTRY}/v0/auth/http",
                  json={"domain": DOMAIN, "timestamp": ts, "signed_timestamp": sig.hex()})
    print("auth:", r.status_code)
    if r.status_code != 200:
        print(r.text[:800])
        sys.exit(1)
    body = r.json()
    token = body.get("registry_token") or body.get("token")
    if not token:
        print("no token in response; keys:", list(body))
        sys.exit(1)
    if "--dry-run" in sys.argv:
        print("logged in; dry run, not publishing")
        sys.exit(0)
    server = json.loads(SERVER_JSON.read_text("utf-8"))
    r = http.post(f"{REGISTRY}/v0.1/publish", json=server,
                  headers={"Authorization": f"Bearer {token}"})
    print("publish:", r.status_code)
    print(r.text[:1500])
