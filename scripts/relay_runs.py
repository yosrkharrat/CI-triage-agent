"""Deliver a repository's workflow runs to a local `ci-triage serve`, by polling.

For trying the service, delegation included, on a machine GitHub cannot reach:
no App, no tunnel, no webhook to configure. Every 20 seconds it lists the
repository's workflow runs and posts each newly completed one, signed the way
GitHub signs a delivery, to the service's /webhook. It also sends a `push` event
when a `ci-triage/fix-*` branch moves, so the service opens the draft PR without
waiting for its own poll.

    set -a; . ./.env.local; set +a        # GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
    uv run python scripts/relay_runs.py owner/repo http://127.0.0.1:8000

Only runs created after it starts are delivered. The service then acts with
GITHUB_TOKEN, which needs `repo` scope to dispatch and open the draft PR.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx

repo, service = sys.argv[1], sys.argv[2].rstrip("/")
token = os.environ["GITHUB_TOKEN"]
secret = os.environ["GITHUB_WEBHOOK_SECRET"].encode()
started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
gh = httpx.Client(
    base_url="https://api.github.com",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    timeout=30,
)
seen: set[tuple[int, int]] = set()
branches: dict[str, str] = {}


def deliver(event: str, payload: dict) -> str:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    r = httpx.post(f"{service}/webhook", content=body, timeout=30, headers={
        "X-GitHub-Event": event, "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": str(uuid.uuid4()), "Content-Type": "application/json",
    })
    return f"{r.status_code} {r.text[:200]}"


print(f"relaying {repo} runs created after {started}Z to {service}", flush=True)
while True:
    try:
        runs = gh.get(f"/repos/{repo}/actions/runs", params={"per_page": 50}).json()
        for run in reversed(runs.get("workflow_runs", [])):
            key = (run["id"], run["run_attempt"])
            if run["status"] != "completed" or key in seen or run["created_at"][:19] < started:
                continue
            seen.add(key)
            out = deliver("workflow_run", {"action": "completed", "workflow_run": run})
            print(f"{datetime.now():%H:%M:%S} run {run['id']} {run['name']!r} "
                  f"{run['head_branch']} {run['conclusion']} -> {out}", flush=True)
        refs = gh.get(f"/repos/{repo}/git/matching-refs/heads/ci-triage/").json()
        for ref in refs if isinstance(refs, list) else []:
            name, sha = ref["ref"], ref["object"]["sha"]
            if branches.get(name) != sha:
                branches[name] = sha
                out = deliver("push", {"ref": name, "after": sha})
                print(f"{datetime.now():%H:%M:%S} push {name} {sha[:8]} -> {out}", flush=True)
    except Exception as exc:  # keep relaying through a blip
        print(f"{datetime.now():%H:%M:%S} relay error: {exc}", flush=True)
    time.sleep(20)
