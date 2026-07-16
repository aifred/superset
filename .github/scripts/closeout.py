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

"""Resolve the author Devin session and close out the Jira ticket on merge."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEVIN_API_BASE = "https://api.devin.ai"


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Devin timestamp that may be an epoch or ISO-8601 string."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def devin_api(
    method: str, path: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    org_id = env_var("DEVIN_ORG_ID")
    token = env_var("DEVIN_API_TOKEN")
    url = f"{DEVIN_API_BASE}/v3/organizations/{org_id}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode()
        raise SystemExit(
            f"Devin API {method} {url} failed {exc.code}: {error_body}"
        ) from exc


def find_sessions(issue_key: str) -> list[dict[str, Any]]:
    # Try bracket-array query format first; this is the most common encoding
    # for Pydantic nested query models in FastAPI.
    from urllib.parse import quote

    # Use 'qs[tags][]' to match sessions that have this issue key in their tags.
    url = f"/sessions?qs[tags][]={quote(issue_key)}"
    response = devin_api("GET", url)
    for key in ("items", "data", "sessions", "nodes"):
        if key in response:
            return response[key]
    # Fallback: empty list instead of failing on unexpected envelope.
    return []


def classify_sessions(
    sessions: list[dict[str, Any]], issue_key: str
) -> dict[str, dict[str, Any] | None]:
    author: dict[str, Any] | None = None
    reviewer: dict[str, Any] | None = None
    fix: dict[str, Any] | None = None

    for session in sessions:
        tags = session.get("tags") or []
        if issue_key not in tags:
            continue
        if "review" in tags:
            reviewer = session
        elif "fix" in tags:
            fix = session
        else:
            # Earliest author session wins, in case duplicates exist.
            session_created = _parse_dt(session.get("created_at"))
            if author is None:
                author = session
            elif session_created is not None:
                author_created = _parse_dt(author.get("created_at"))
                if author_created is None or session_created < author_created:
                    author = session
    return {"author": author, "reviewer": reviewer, "fix": fix}


def terminate_session(session: dict[str, Any]) -> None:
    session_id = session.get("session_id") or session.get("devin_id")
    if not session_id:
        return
    devin_api("DELETE", f"/sessions/{session_id}?archive=true")
    print(f"Terminated author session {session_id}")


def format_cycle_time(author_created_at: int | float | str, merged_at: str) -> str:
    merged_dt = _parse_dt(merged_at)
    if merged_dt is None:
        return "unknown"
    created_dt = _parse_dt(author_created_at)
    if created_dt is None:
        return "unknown"
    delta = merged_dt - created_dt
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, remainder = divmod(total_minutes, 1440)
    hours = remainder // 60
    return f"{days}d {hours}h"


def jira_auth_header() -> str:
    email = env_var("JIRA_USER_EMAIL")
    token = env_var("JIRA_API_TOKEN")
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"


def jira_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base = env_var("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3{path}"
    headers = {
        "Authorization": jira_auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode()
        raise SystemExit(
            f"Jira {method} {url} failed {exc.code}: {error_body}"
        ) from exc


def load_field_ids() -> dict[str, str | None]:
    """Load Jira customfield IDs from .github/jira-fields.json or env vars."""
    field_names = [
        "acus_consumed",
        "cycle_time",
        "review_verdict",
        "review_confidence",
        "reviewer_session_id",
        "author_session_id",
        "merged_by",
    ]
    config_path = os.environ.get("JIRA_FIELDS_FILE", ".github/jira-fields.json")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            file_config = json.load(f)
        return {name: file_config.get(name) for name in field_names}

    return {name: os.environ.get(f"JIRA_FIELD_{name.upper()}") for name in field_names}


def jira_field_type(field_id: str) -> str:
    fields = jira_request("GET", "/field")
    for field in fields:
        if field.get("id") == field_id:
            schema = field.get("schema", {})
            return schema.get("type", "string")
    return "string"


def format_field_value(value: Any, field_type: str) -> Any:
    if field_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return str(value)


def transition_to_done(issue_key: str) -> None:
    preferred = os.environ.get("JIRA_DONE_TRANSITION_NAME", "Done")
    data = jira_request(
        "GET", f"/issue/{issue_key}/transitions?expand=transitions.fields"
    )
    transitions = data.get("transitions", [])
    chosen = None
    for transition in transitions:
        to = transition.get("to", {})
        category = to.get("statusCategory", {})
        if transition.get("name") == preferred or category.get("key") == "done":
            chosen = transition
            break
    if not chosen:
        print("No 'Done' transition found; skipping Jira transition")
        return
    jira_request(
        "POST", f"/issue/{issue_key}/transitions", {"transition": {"id": chosen["id"]}}
    )
    to_name = chosen.get("to", {}).get("name") or "unknown"
    print(f"Transitioned {issue_key} to {to_name}")


def update_jira_fields(
    issue_key: str,
    author: dict[str, Any] | None,
    reviewer: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
    merged_at: str,
    merged_by: str,
) -> None:
    field_ids = load_field_ids()
    if not any(field_ids.values()):
        print("No Jira field IDs configured; skipping Jira field update")
        return

    acus = sum(
        float(s.get("acus_consumed") or 0)
        for s in sessions
        if s.get("acus_consumed") is not None
    )
    if author and author.get("created_at"):
        cycle_time = format_cycle_time(author["created_at"], merged_at)
    else:
        cycle_time = "unknown"

    review_output = reviewer.get("structured_output") or {} if reviewer else {}
    values = {
        "acus_consumed": acus,
        "cycle_time": cycle_time,
        "review_verdict": review_output.get("verdict", ""),
        "review_confidence": review_output.get("confidence", ""),
        "reviewer_session_id": reviewer.get("session_id") if reviewer else "",
        "author_session_id": author.get("session_id") if author else "",
        "merged_by": merged_by,
    }

    fields_payload = {}
    type_cache = {}
    for name, field_id in field_ids.items():
        if not field_id:
            continue
        if field_id not in type_cache:
            type_cache[field_id] = jira_field_type(field_id)
        fields_payload[field_id] = format_field_value(
            values.get(name, ""), type_cache[field_id]
        )

    jira_request("PUT", f"/issue/{issue_key}", {"fields": fields_payload})
    print(f"Updated custom fields on {issue_key}")


def post_github_comment(body: str) -> None:
    owner, repo = env_var("REPO").split("/")
    pr = env_var("PR_NUMBER")
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("No GH_TOKEN; skipping GitHub comment")
        return
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr}/comments"
    req = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"GitHub comment failed: {exc.read().decode()}", file=sys.stderr)


def main() -> int:
    issue_key = env_var("ISSUE_KEY")
    merged_at = env_var("MERGED_AT")
    merged_by = os.environ.get("MERGED_BY", "")

    sessions = find_sessions(issue_key)
    classified = classify_sessions(sessions, issue_key)
    author = classified["author"]
    reviewer = classified["reviewer"]

    if not author:
        print(
            f"No author session found for {issue_key}; skipping closeout",
            file=sys.stderr,
        )
        return 0

    terminate_session(author)

    # Only transition and write fields if Jira credentials are configured.
    if (
        os.environ.get("JIRA_BASE_URL")
        and os.environ.get("JIRA_USER_EMAIL")
        and os.environ.get("JIRA_API_TOKEN")
    ):
        transition_to_done(issue_key)
        update_jira_fields(issue_key, author, reviewer, sessions, merged_at, merged_by)
    else:
        print("Jira credentials not configured; skipping Jira close-out")

    body = (
        f"Auto-ship close-out for {issue_key} complete.\n\n"
        f"- Author session: {author.get('session_id')}\n"
        f"- Reviewer session: {reviewer.get('session_id') if reviewer else 'n/a'}\n"
        f"- Merged by: {merged_by or 'n/a'}\n"
    )
    post_github_comment(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
