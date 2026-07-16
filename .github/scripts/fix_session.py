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

"""Start and poll a Devin fix session for the auto-ship pipeline."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

API_BASE = "https://api.devin.ai"
MAX_POLL_MINUTES = 30
POLL_INTERVAL_SECONDS = 30

FIX_PROMPT = """You are a fix engineer for the Apache Superset auto-ship pipeline.

Branch / Jira key: {issue_key}
Title: {pr_title}
PR URL: {pr_url}

PR body:
{pr_body}

A reviewer requested changes on this pull request. Address all unresolved review comments, push the minimal fix commits to the PR branch, and keep the change set small.

Do not modify CODEOWNERS-forbidden paths, CI configuration, dependency lock files, or security-sensitive files unless the review explicitly asks for those changes.

When finished, push the final changes and report a short summary of what you changed.
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


def start_fix() -> str:
    issue_key = env_var("ISSUE_KEY")
    pr_title = os.environ.get("PR_TITLE", "")
    pr_body = os.environ.get("PR_BODY", "")
    pr_url = os.environ.get("PR_URL", "")

    prompt = FIX_PROMPT.format(
        issue_key=issue_key,
        pr_title=pr_title,
        pr_url=pr_url,
        pr_body=pr_body or "(no body)",
    )

    payload = {
        "tags": [issue_key, "fix"],
        "max_acu_limit": int(os.environ.get("MAX_ACU_LIMIT", 15)),
        "prompt": prompt,
        "bypass_approval": True,
        "repos": [env_var("REPO")],
    }

    response = api_request("POST", "/sessions", payload)
    session_id = response.get("session_id") or response.get("devin_id")
    if not session_id:
        raise SystemExit(f"No session_id in Devin response: {response}")

    with open("fix_session.json", "w", encoding="utf-8") as f:
        json.dump(response, f)

    print(f"Started fix session: {session_id}")
    return session_id


def poll_session() -> dict[str, Any]:
    with open("fix_session.json", encoding="utf-8") as f:
        session = json.load(f)

    session_id = session.get("session_id") or session.get("devin_id")
    if not session_id:
        raise SystemExit("fix_session.json has no session_id")

    deadline = time.time() + MAX_POLL_MINUTES * 60
    while time.time() < deadline:
        data = api_request("GET", f"/sessions/{session_id}")
        status = data.get("status", "")
        status_detail = data.get("status_detail", "")
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] fix session {session_id} "
            f"status: {status} detail: {status_detail}"
        )

        if status_detail in {"waiting_for_user", "waiting_for_approval"}:
            raise SystemExit(
                f"Fix session {session_id} is waiting for user input/approval"
            )

        if status in {"exit", "error"}:
            with open("fix_session.json", "w", encoding="utf-8") as f:
                json.dump(data, f)
            if status == "exit":
                output = data.get("structured_output") or data.get("output") or {}
                with open("fix_output.json", "w", encoding="utf-8") as f:
                    json.dump(output, f)
                print("Fix session completed; output written to fix_output.json")
                return data
            raise SystemExit(f"Fix session ended with status: {status}")

        if status == "suspended":
            print(f"Fix session {session_id} suspended; continuing to poll")

        time.sleep(POLL_INTERVAL_SECONDS)

    raise SystemExit("Timeout waiting for fix session")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "start":
        start_fix()
    elif command == "poll":
        poll_session()
    else:
        raise SystemExit("Usage: fix_session.py {start|poll}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
