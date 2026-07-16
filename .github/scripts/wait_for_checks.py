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

"""Wait for a PR's other status checks to finish before the policy gate runs.

The deterministic policy gate treats any not-yet-completed check as a failure.
Because the gate is triggered by the reviewer verdict comment, the longer
CI jobs (unit tests, DB tests, docker builds) are often still running at that
moment, so the gate would escalate to a human purely because of timing.

This script polls the PR's status-check rollup until every check other than
``devin-review`` has reached a terminal state, or until a timeout elapses. It
is best-effort: it always exits 0 so the policy gate still runs afterwards and
makes the authoritative pass/fail decision on the settled results.
"""

import json
import os
import subprocess
import sys
import time
from typing import Any

# States that mean a check has not reached a terminal conclusion yet.
PENDING_STATUSES = {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}

POLL_INTERVAL_SECONDS = int(os.environ.get("WAIT_POLL_INTERVAL", "20"))
TIMEOUT_SECONDS = int(os.environ.get("WAIT_TIMEOUT_SECONDS", "1800"))


def _rollup() -> list[dict[str, Any]]:
    repo, pr = os.environ["GITHUB_REPOSITORY"], os.environ["PR_NUMBER"]
    out = subprocess.run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "statusCheckRollup"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GH_TOKEN": os.environ["GH_TOKEN"]},
    )
    raw = json.loads(out.stdout).get("statusCheckRollup") or []
    if isinstance(raw, dict):
        raw = raw.get("nodes") or []
    return [c for c in raw if (c.get("name") or c.get("context")) != "devin-review"]


def _is_pending(check: dict[str, Any]) -> bool:
    """Return True when a check has not reached a terminal state yet."""
    if (status := check.get("status")) is not None:
        return str(status).upper() in PENDING_STATUSES
    # StatusContext rows expose ``state`` rather than ``status``.
    return str(check.get("state", "")).upper() == "PENDING"


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        checks = _rollup()
        pending = [
            c.get("name") or c.get("context") or "unknown"
            for c in checks
            if _is_pending(c)
        ]
        if not pending:
            print(f"All {len(checks)} non-review checks have completed.")
            return 0
        if time.monotonic() >= deadline:
            print(
                "WARN: timed out after "
                f"{TIMEOUT_SECONDS}s waiting for checks: {', '.join(sorted(pending))}. "
                "Running the policy gate against the current state.",
                file=sys.stderr,
            )
            return 0
        print(f"Waiting for {len(pending)} check(s): {', '.join(sorted(pending))}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
