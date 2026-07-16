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

"""Deterministic auto-ship merge policy gate."""

import json
import os
import re
import subprocess
import sys
from typing import Any

FORBIDDEN = [
    "superset/migrations/",
    "superset/security/",
    "superset/config.py",
    ".github/workflows/",
    "helm/",
    "docker/",
    "requirements/",
    "RELEASING/",
    "scripts/",
]
LOCK_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|Pipfile\.lock|uv\.lock)$|.*\.lock$"
)


def _check_state(c: dict[str, Any]) -> bool:
    """Return True if a single status context/check is considered passing."""
    status = c.get("status")
    conclusion = c.get("conclusion")

    if status == "COMPLETED":
        return conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED")
    if state := c.get("state"):
        if state == "PENDING":
            return False
        return state in ("SUCCESS", "EXPECTED")
    return False


def gh(fields: str) -> dict[str, Any]:
    repo, pr = os.environ["GITHUB_REPOSITORY"], os.environ["PR_NUMBER"]
    out = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", fields],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GH_TOKEN": os.environ["GH_TOKEN"]},
    )
    return json.loads(out.stdout)


def required_status_checks(pr_node_id: str) -> list[dict[str, Any]] | None:
    """Fetch checks with isRequired populated via GitHub GraphQL.

    Returns None when the GraphQL call fails, so callers can fall back to the
    REST-shaped statusCheckRollup from `gh pr view`. Returns an empty list
    only when the call succeeded but no contexts were returned.
    """
    query = """
    query($prId: ID!) {
      node(id: $prId) {
        ... on PullRequest {
          commits(last: 1) {
            nodes {
              commit {
                statusCheckRollup {
                  contexts(first: 100) {
                    nodes {
                      ... on CheckRun {
                        name
                        status
                        conclusion
                        isRequired(pullRequestId: $prId)
                      }
                      ... on StatusContext {
                        context
                        state
                        isRequired(pullRequestId: $prId)
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=" + query, "-f", f"prId={pr_node_id}"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN") or ""},
    )
    if proc.returncode != 0:
        print(f"GraphQL warning: {proc.stderr.strip()}", file=sys.stderr)
        return None

    data = json.loads(proc.stdout)
    if "errors" in data:
        print(f"GraphQL errors: {data['errors']}", file=sys.stderr)
        return None
    contexts = (
        data.get("data", {})
        .get("node", {})
        .get("commits", {})
        .get("nodes", [{}])[0]
        .get("commit", {})
        .get("statusCheckRollup", {})
        .get("contexts", {})
        .get("nodes", [])
    )

    # Normalize StatusContext shape into the same keys CheckRun uses.
    normalized: list[dict[str, Any]] = []
    for c in contexts:
        name = c.get("name") or c.get("context") or "unknown"
        if name == "devin-review":
            continue
        normalized.append(
            {
                "name": name,
                "status": c.get("status"),
                "conclusion": c.get("conclusion"),
                "state": c.get("state"),
                "isRequired": c.get("isRequired"),
            }
        )
    return normalized


def fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    if gh_output := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"policy_reason={msg}\n")
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"- Policy gate failed: {msg}\n")
    return 1


def main() -> int:
    verdict = json.load(
        open(os.environ.get("DEVIN_VERDICT") or sys.argv[1], encoding="utf-8")
    )
    if (actual_verdict := verdict.get("verdict")) != "approve":
        return fail(f"verdict is '{actual_verdict}' (must be 'approve')")
    if (confidence := float(verdict.get("confidence", 0))) < 0.8:
        return fail(f"confidence is {confidence} (must be >= 0.8)")
    if (risk_tier := str(verdict.get("risk_tier", "")).lower()) != "low":
        return fail(f"risk_tier is '{risk_tier}' (must be 'low')")
    if not verdict.get("tests_adequate"):
        return fail("tests_adequate is false")

    pr = gh("headRefName,title,additions,deletions,files,statusCheckRollup,id")
    title = pr.get("title", "")
    branch = pr.get("headRefName", "")
    if not re.search(r"\b[A-Z][A-Z0-9]*-\d+\b", f"{title} {branch}"):
        return fail(f"no Jira issue key in PR title ('{title}') or branch ('{branch}')")

    for f in pr.get("files", []):
        path = f.get("path", "")
        if any(path.startswith(p) for p in FORBIDDEN):
            return fail(f"forbidden path touched: {path}")
        if LOCK_RE.search(path):
            return fail(f"lock/dependency file touched: {path}")

    checks: list[dict[str, Any]]
    if pr_node_id := os.environ.get("PR_NODE_ID") or pr.get("id"):
        ghql_checks = required_status_checks(pr_node_id)
        checks = ghql_checks if ghql_checks is not None else []
    else:
        checks = []

    if not checks:
        raw = pr.get("statusCheckRollup") or []
        if isinstance(raw, dict):
            raw = raw.get("nodes") or []
        checks = [
            c for c in raw if (c.get("name") or c.get("context")) != "devin-review"
        ]

    required = [c for c in checks if c.get("isRequired")]
    to_check = required if required else checks
    if not to_check:
        return fail("no required or available status checks to verify")
    for c in to_check:
        name = c.get("name") or c.get("context") or "unknown"
        if not _check_state(c):
            status = c.get("status") or c.get("state") or "unknown"
            conclusion = c.get("conclusion") or "unknown"
            return fail(
                f"required check '{name}' did not pass (status={status}, conclusion={conclusion})"
            )

    diff_size = int(pr.get("additions", 0)) + int(pr.get("deletions", 0))
    if diff_size >= 300:
        return fail(f"diff is {diff_size} lines (must be < 300)")

    print("PASS: policy gate satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
