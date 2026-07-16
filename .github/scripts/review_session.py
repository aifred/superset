#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to you under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Start and poll a Devin reviewer session for the auto-ship pipeline."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

API_BASE = "https://api.devin.ai"
MAX_POLL_MINUTES = 15
POLL_INTERVAL_SECONDS = 30


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "request_changes", "needs_human"],
        },
        "findings": {"type": "string"},
        "risk_tier": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "tests_adequate": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["verdict", "findings", "risk_tier", "tests_adequate", "confidence"],
}


REVIEWER_PROMPT = """You are reviewing an auto-ship pull request for the Apache Superset repo.

Use the review checklist in AGENTS.md under "Auto-ship Pipeline (pilot scope)" -> "Reviewer checklist (3c)".

Branch / Jira key: {issue_key}
Title: {pr_title}
PR URL: {pr_url}

PR body:
{pr_body}

Diff:
{diff}

Read the diff carefully, as an independent reviewer with no prior knowledge of the author's intent.

Return your evaluation as structured_output with these fields:
- verdict: one of approve, request_changes, or needs_human
- findings: concise summary of what you checked and what you found
- risk_tier: low, medium, or high
- tests_adequate: true/false
- confidence: number between 0 and 1

A PR is eligible for auto-merge only when verdict == approve, confidence >= 0.8, risk_tier == low, the diff is < 300 lines, and CI is green. CODEOWNERS enforces the forbidden paths, not this check, but you should still note any forbidden-path changes in findings.
"""

_VERDICT_BLOCK = '{{"sha":"{head_sha}","run_id":"{run_id}","verdict":"<verdict>","findings":"<findings>","risk_tier":"<risk_tier>","tests_adequate":<true|false>,"confidence":<confidence>}}'  # noqa: E501

POST_INSTRUCTIONS = """
When you have finalized your verdict:
1. Call `provide_structured_output` with the verdict JSON.
2. Create a file named `comment.md` with exactly this content structure,
   replacing the `<...>` placeholders with your actual verdict values:

### Devin review verdict

```json
{verdict_json}
```

<your concise findings summary>

3. Run this exact command to post the verdict back to GitHub:
gh pr comment {pr_number} --repo {repo} --body-file comment.md

The `GH_TOKEN` environment variable is already set in your session for `gh`.
"""


def env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def api_request(
    method: str, path: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    org_id = env_var("DEVIN_ORG_ID")
    token = env_var("DEVIN_API_TOKEN")
    url = f"{API_BASE}/v3/organizations/{org_id}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode()
        raise SystemExit(
            f"Devin API {method} {url} failed {exc.code}: {error_body}"
        ) from exc


def fetch_diff() -> str:
    repo = env_var("REPO")
    pr = env_var("PR_NUMBER")
    gh_token = env_var("GH_TOKEN")
    env = {**os.environ, "GH_TOKEN": gh_token}
    result = subprocess.run(
        ["gh", "pr", "diff", pr, "--repo", repo],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"gh pr diff failed: {result.stderr}", file=sys.stderr)
        return "(diff unavailable)"
    # Keep the prompt under the ~64KB schema+prompt budget, leaving headroom.
    return result.stdout[:50000]


def _session_secrets() -> list[dict[str, Any]]:
    """Expose PR and GitHub token to the reviewer session so it can post a verdict comment."""
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("PR_HEAD_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")

    secrets = []
    if token:
        secrets.append({"key": "GH_TOKEN", "value": token, "sensitive": True})
    for key, value in (
        ("REPO", repo),
        ("PR_NUMBER", pr_number),
        ("HEAD_SHA", head_sha),
        ("RUN_ID", run_id),
    ):
        if value:
            secrets.append({"key": key, "value": value, "sensitive": False})
    return secrets


def start_review() -> str:
    issue_key = env_var("ISSUE_KEY")
    pr_title = os.environ.get("PR_TITLE", "")
    pr_body = os.environ.get("PR_BODY", "")
    pr_url = os.environ.get("PR_URL", "")
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("PR_HEAD_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    diff = fetch_diff()

    prompt = REVIEWER_PROMPT.format(
        issue_key=issue_key,
        pr_title=pr_title,
        pr_url=pr_url,
        pr_body=pr_body or "(no body)",
        diff=diff,
    )

    if head_sha and run_id and pr_number and repo:
        verdict_json = _VERDICT_BLOCK.format(head_sha=head_sha, run_id=run_id)
        prompt += POST_INSTRUCTIONS.format(
            pr_number=pr_number,
            repo=repo,
            verdict_json=verdict_json,
        )

    payload: dict[str, Any] = {
        "tags": [issue_key, "review", f"run-{run_id}"]
        if run_id
        else [issue_key, "review"],
        "max_acu_limit": int(os.environ.get("MAX_ACU_LIMIT", 10)),
        "prompt": prompt,
        "bypass_approval": True,
        "structured_output_required": True,
        "structured_output_schema": VERDICT_SCHEMA,
        "repos": [env_var("REPO")],
    }

    if session_secrets := _session_secrets():
        payload["session_secrets"] = session_secrets

    response = api_request("POST", "/sessions", payload)
    session_id = response.get("session_id") or response.get("devin_id")
    if not session_id:
        raise SystemExit(f"No session_id in Devin response: {response}")

    with open("session.json", "w", encoding="utf-8") as f:
        json.dump(response, f)

    print(f"Started reviewer session: {session_id}")
    return session_id


def save_verdict(session: dict[str, Any]) -> None:
    structured = session.get("structured_output") or {}
    with open("verdict.json", "w", encoding="utf-8") as f:
        json.dump(structured, f)


def poll_session() -> dict[str, Any]:
    with open("session.json", encoding="utf-8") as f:
        session = json.load(f)

    session_id = session.get("session_id") or session.get("devin_id")
    if not session_id:
        raise SystemExit("session.json has no session_id")

    deadline = time.time() + MAX_POLL_MINUTES * 60
    while time.time() < deadline:
        data = api_request("GET", f"/sessions/{session_id}")
        status = data.get("status", "")
        status_detail = data.get("status_detail", "")
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] session {session_id} "
            f"status: {status} detail: {status_detail}"
        )

        if status_detail in {"waiting_for_user", "waiting_for_approval"}:
            raise SystemExit(
                f"Reviewer session {session_id} is waiting for user input/approval"
            )

        if status in {"exit", "error"}:
            if status == "exit":
                save_verdict(data)
                print("Session completed; verdict written to verdict.json")
                return data
            raise SystemExit(f"Reviewer session ended with status: {status}")

        if status == "suspended":
            print("Reviewer session suspended; continuing to poll")

        time.sleep(POLL_INTERVAL_SECONDS)

    raise SystemExit("Timeout waiting for reviewer session")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "start":
        start_review()
    elif command == "poll":
        poll_session()
    else:
        raise SystemExit("Usage: review_session.py {start|poll}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
