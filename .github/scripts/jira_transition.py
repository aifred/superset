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

"""Transition a Jira issue to a target status by name (e.g. Backlog)."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def basic_auth() -> str:
    email = env_var("JIRA_USER_EMAIL")
    token = env_var("JIRA_API_TOKEN")
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"


def api_request(
    base_url: str,
    auth: str,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode()
        raise SystemExit(
            f"Jira {method} {url} failed {exc.code}: {error_body}"
        ) from exc


def find_transition_id(
    transitions: list[dict[str, Any]], target_name: str
) -> str | None:
    target_lower = target_name.lower()
    for transition in transitions:
        to = transition.get("to") or {}
        name = (to.get("name") or "").lower()
        if name == target_lower:
            return transition.get("id")
    # Fallback to status category key "new" for Backlog-like statuses.
    for transition in transitions:
        to = transition.get("to") or {}
        if (to.get("statusCategory") or {}).get("key") == "new":
            return transition.get("id")
    return None


def transition_issue(
    issue_key: str, target_name: str, comment: str | None = None
) -> None:
    base_url = env_var("JIRA_BASE_URL")
    auth = basic_auth()

    data = api_request(
        base_url,
        auth,
        "GET",
        f"/rest/api/3/issue/{issue_key}/transitions?expand=transitions.fields",
    )
    if not isinstance(data, dict):
        raise SystemExit("Unexpected Jira transitions response")

    transitions = data.get("transitions") or []
    transition_id = find_transition_id(transitions, target_name)
    if not transition_id:
        print(
            f"No '{target_name}' transition found for {issue_key}; skipping",
            file=sys.stderr,
        )
        return

    payload: dict[str, Any] = {"transition": {"id": transition_id}}
    if comment:
        payload["update"] = {"comment": [{"add": {"body": comment}}]}

    api_request(
        base_url, auth, "POST", f"/rest/api/3/issue/{issue_key}/transitions", payload
    )
    print(f"Transitioned {issue_key} to '{target_name}'")


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: jira_transition.py <issue-key> <target-status> [comment]",
            file=sys.stderr,
        )
        return 1
    issue_key = sys.argv[1]
    target_name = sys.argv[2]
    comment = sys.argv[3] if len(sys.argv) > 3 else None
    transition_issue(issue_key, target_name, comment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
