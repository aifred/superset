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

"""Parse a Devin review-verdict PR comment and set GitHub Actions outputs."""

import json
import os
import re
import subprocess
import sys
from typing import Any

VERDICT_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def set_output(name: str, value: str) -> None:
    """Write a key=value line to GITHUB_OUTPUT if available."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def pr_view(fields: str) -> dict[str, Any]:
    """Fetch PR metadata via the gh CLI."""
    repo = env_var("REPO")
    pr = env_var("PR_NUMBER")
    token = os.environ.get("GH_TOKEN")
    proc = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", fields],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": token or ""},
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh pr view failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def extract_issue_key(pr: dict[str, Any]) -> str:
    text = f"{pr.get('title', '')} {pr.get('headRefName', '')}"
    m = re.search(r"\b([A-Z][A-Z0-9]*-\d+)\b", text)
    return m.group(1) if m else ""


def parse_verdict_comment(body: str) -> dict[str, Any]:
    match = VERDICT_BLOCK_RE.search(body)
    if not match:
        raise SystemExit("No JSON verdict block found in PR comment")

    raw = match.group(1).strip()
    verdict = json.loads(raw)
    for field in ("verdict", "sha", "run_id"):
        if field not in verdict:
            raise SystemExit(f"Verdict JSON missing required field: {field}")
    return verdict


def write_verdict(verdict: dict[str, Any]) -> None:
    with open("verdict.json", "w", encoding="utf-8") as f:
        json.dump(verdict, f)


def _write_outputs(
    valid: str,
    stale: str,
    verdict: str,
    comment_sha: str,
    run_id: str,
    pr_head_sha: str,
    node_id: str,
    issue_key: str,
) -> None:
    set_output("valid", valid)
    set_output("stale", stale)
    set_output("verdict", verdict)
    set_output("sha", comment_sha)
    set_output("run_id", run_id)
    set_output("pr_head_sha", pr_head_sha)
    set_output("node_id", node_id)
    set_output("issue_key", issue_key)
    print(f"valid={valid} stale={stale} verdict={verdict}")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command != "parse":
        raise SystemExit("Usage: review_complete.py parse")

    body = os.environ.get("COMMENT_BODY") or ""
    stale = "false"
    valid = "false"
    verdict_value = ""
    comment_sha = ""
    run_id = ""
    pr_head_sha = ""
    node_id = ""
    issue_key = ""

    try:
        pr = pr_view("id,headRefOid,headRefName,title")
        pr_head_sha = pr.get("headRefOid") or ""
        node_id = pr.get("id") or ""
        issue_key = extract_issue_key(pr)

        verdict = parse_verdict_comment(body)
        write_verdict(verdict)
        valid = "true"
        verdict_value = str(verdict.get("verdict", ""))
        comment_sha = str(verdict.get("sha", ""))
        run_id = str(verdict.get("run_id", ""))
        if comment_sha and pr_head_sha and comment_sha != pr_head_sha:
            stale = "true"
    except Exception as exc:
        print(f"Verdict parse failed: {exc}", file=sys.stderr)

    _write_outputs(
        valid,
        stale,
        verdict_value,
        comment_sha,
        run_id,
        pr_head_sha,
        node_id,
        issue_key,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
