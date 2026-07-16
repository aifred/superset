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


def fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def main() -> int:
    verdict = json.load(
        open(os.environ.get("DEVIN_VERDICT") or sys.argv[1], encoding="utf-8")
    )
    if verdict.get("verdict") != "approve":
        return fail("verdict is not 'approve'")
    if float(verdict.get("confidence", 0)) < 0.8:
        return fail("confidence < 0.8")
    if str(verdict.get("risk_tier", "")).lower() != "low":
        return fail("risk_tier is not 'low'")
    if not verdict.get("tests_adequate"):
        return fail("tests_adequate is false")

    pr = gh("headRefName,title,statusCheckRollup,additions,deletions,files")
    text = f"{pr.get('title', '')} {pr.get('headRefName', '')}"
    if not re.search(r"\b[A-Z][A-Z0-9]*-\d+\b", text):
        return fail("no Jira issue key in PR title or branch")

    for f in pr.get("files", []):
        path = f.get("path", "")
        if any(path.startswith(p) for p in FORBIDDEN) or LOCK_RE.match(path):
            return fail(f"forbidden path touched: {path}")

    checks = pr.get("statusCheckRollup") or []
    required = [c for c in checks if c.get("isRequired")]
    to_check = required if required else checks
    for c in to_check:
        name = c.get("name") or c.get("context") or "unknown"
        status = c.get("status")
        conclusion = c.get("conclusion")
        state = c.get("state")

        if status == "COMPLETED":
            if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                return fail(f"check '{name}' conclusion={conclusion}")
        elif state:
            if state == "PENDING":
                return fail(f"check '{name}' not completed")
            if state not in ("SUCCESS", "EXPECTED"):
                return fail(f"check '{name}' state={state}")
        else:
            return fail(f"check '{name}' has no status or state")

    if int(pr.get("additions", 0)) + int(pr.get("deletions", 0)) >= 300:
        return fail("diff >= 300 lines")

    print("PASS: policy gate satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
