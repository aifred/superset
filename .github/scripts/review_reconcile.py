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

"""Reconcile the auto-ship gate across the reviewer verdict and CI completion.

The gate has two independent inputs that can arrive in either order:

* the Devin reviewer verdict, posted as a PR comment (``issue_comment`` event);
* the CI result for the PR head commit (``workflow_run`` completion events).

Instead of trying to make one event wait for the other, this workflow re-runs on
*both* kinds of event and reconciles the current state each time: it resolves the
PR, reads the latest authorized verdict comment, and computes the aggregate
status of the branch-protection *required* checks. The gate only proceeds when
both inputs are ready; otherwise it exits quietly and a later event re-checks.

This makes the outcome independent of event ordering and needs no polling.
"""

import json
import os
import subprocess
import sys
import time
from typing import Any

# GitHub's status-check rollup can briefly lag the workflow_run event that
# triggers us, so a just-completed required check may still read as pending.
# Re-query a bounded number of times to let it settle before we decide to wait
# (this is not a poll for *other* checks — it only lets the rollup catch up).
_ROLLUP_SETTLE_RETRIES = 3
_ROLLUP_SETTLE_DELAY_SECONDS = 10

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import policy  # noqa: E402  (local module, resolved via sys.path above)
import review_complete  # noqa: E402


def _gh_json(args: list[str]) -> Any:
    """Run a gh command that emits JSON and return the parsed payload."""
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")},
    )
    if proc.returncode != 0:
        print(f"gh command failed ({' '.join(args)}): {proc.stderr.strip()}")
        return None
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)


def set_output(name: str, value: str) -> None:
    review_complete.set_output(name, value)


def _is_pending(c: dict[str, Any]) -> bool:
    """Return True if a check has not reached a terminal state yet.

    A CheckRun is pending until its ``status`` is ``COMPLETED``; a legacy
    StatusContext is pending while its ``state`` is ``PENDING``.
    """
    if (status := c.get("status")) is not None:
        return status != "COMPLETED"
    return c.get("state") == "PENDING"


def _resolve_pr_number(repo: str) -> str:
    """Return the PR number for the triggering event or explicit input, or ''.

    A scheduled/`workflow_call` sweep passes the target PR directly via
    ``INPUT_PR_NUMBER`` so the same reconcile path can heal a PR whose
    ``workflow_run`` completion event was never delivered. When set, it takes
    precedence over event-derived resolution.
    """
    if explicit := os.environ.get("INPUT_PR_NUMBER", "").strip():
        return explicit

    event = os.environ.get("EVENT_NAME", "")

    if event == "issue_comment":
        # Only comments on pull requests are relevant.
        if os.environ.get("IS_PULL_REQUEST") != "true":
            return ""
        return os.environ.get("ISSUE_NUMBER", "")

    if event == "workflow_run":
        # ``pull_requests`` is populated only for same-repo runs and is often
        # empty, so fall back to resolving the PR from the head commit.
        raw = os.environ.get("WF_PULL_REQUESTS", "")
        if raw:
            try:
                prs = json.loads(raw)
            except json.JSONDecodeError:
                prs = []
            if prs:
                return str(prs[0].get("number", ""))

        sha = os.environ.get("WF_HEAD_SHA", "")
        if not sha:
            return ""
        pulls = _gh_json(
            ["gh", "api", f"repos/{repo}/commits/{sha}/pulls", "--jq", "."]
        )
        if isinstance(pulls, list):
            for pr in pulls:
                if pr.get("state") == "open":
                    return str(pr.get("number", ""))
            if pulls:
                return str(pulls[0].get("number", ""))
        return ""

    return ""


def _latest_verdict_comment(repo: str, pr_number: str) -> dict[str, Any] | None:
    """Return the most recent authorized Devin verdict comment, if any."""
    comments = _gh_json(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{pr_number}/comments",
        ]
    )
    if not isinstance(comments, list):
        return None

    allowed = review_complete._allowed_commenters()
    for comment in reversed(comments):
        user = comment.get("user") or {}
        if user.get("type") != "Bot" or user.get("login") not in allowed:
            continue
        body = comment.get("body") or ""
        if "### Devin review verdict" not in body:
            continue
        return comment
    return None


def _fetch_pr(repo: str, pr_number: str) -> Any:
    """Fetch the PR fields needed to reconcile the gate."""
    return _gh_json(
        [
            "gh",
            "pr",
            "view",
            pr_number,
            "--repo",
            repo,
            "--json",
            "id,headRefOid,headRefName,title,statusCheckRollup,labels",
        ]
    )


def _is_escalated(pr: dict[str, Any]) -> bool:
    """Return True if the PR already carries the needs-human escalation label."""
    labels = pr.get("labels") or []
    return any(label.get("name") == "needs-human" for label in labels)


def _ci_status(node_id: str, pr: dict[str, Any]) -> str:
    """Return 'success', 'failure', or 'pending' for the required checks.

    Reuses the policy gate's check helpers so the definition of "required" and
    "passing" stays identical to the deterministic gate that runs afterwards.
    """
    checks: list[dict[str, Any]]
    ghql_checks = policy.required_status_checks(node_id) if node_id else None
    checks = ghql_checks if ghql_checks is not None else []

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
        # No checks have registered yet (the gate's own devin-review check is
        # excluded). Treat this as still-pending rather than success so a fast
        # verdict landing before CI appears waits for a later event instead of
        # being handed to the deterministic gate, which fails closed on an empty
        # check set and would prematurely escalate to human review.
        return "pending"

    pending = False
    for c in to_check:
        if _is_pending(c):
            pending = True
            continue
        if not policy._check_state(c):
            return "failure"
    return "pending" if pending else "success"


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]

    has_verdict = "false"
    verdict_value = ""
    stale = "false"
    issue_key = ""
    node_id = ""
    head_sha = ""
    ci_status = "pending"
    escalated = "false"

    pr_number = _resolve_pr_number(repo)
    if not pr_number:
        print("No pull request resolved for this event; nothing to reconcile.")
        _emit(pr_number, has_verdict, verdict_value, stale, issue_key, ci_status)
        return 0

    pr = _fetch_pr(repo, pr_number)
    if not isinstance(pr, dict):
        print(f"Could not load PR #{pr_number}; skipping.")
        _emit(pr_number, has_verdict, verdict_value, stale, issue_key, ci_status)
        return 0

    node_id = pr.get("id") or ""
    head_sha = pr.get("headRefOid") or ""
    issue_key = review_complete.extract_issue_key(pr)
    escalated = "true" if _is_escalated(pr) else "false"

    # For workflow_run events (not the explicit-pr sweep path), ignore CI that
    # ran against a superseded commit; a newer push will produce its own
    # completion event.
    wf_sha = os.environ.get("WF_HEAD_SHA", "")
    if (
        os.environ.get("EVENT_NAME") == "workflow_run"
        and not os.environ.get("INPUT_PR_NUMBER")
        and wf_sha
        and wf_sha != head_sha
    ):
        print(
            f"workflow_run head {wf_sha} != PR head {head_sha}; waiting for newer CI."
        )
        _emit(pr_number, has_verdict, verdict_value, stale, issue_key, ci_status)
        return 0

    if (comment := _latest_verdict_comment(repo, pr_number)) is not None:
        try:
            verdict = review_complete.parse_verdict_comment(comment.get("body") or "")
            review_complete.write_verdict(verdict)
            has_verdict = "true"
            verdict_value = str(verdict.get("verdict", ""))
            comment_sha = str(verdict.get("sha", ""))
            if comment_sha and head_sha and comment_sha != head_sha:
                stale = "true"
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            print(f"Failed to parse verdict comment: {exc}")

    ci_status = _ci_status(node_id, pr)

    # On a workflow_run event a required check has just completed, so a pending
    # reading is most likely rollup lag: re-query briefly to let it settle
    # rather than exiting quietly and waiting for an unrelated later event. Skip
    # this when an explicit PR number is provided (sweep path), because the
    # triggering event may refer to a different PR.
    if (
        ci_status == "pending"
        and os.environ.get("EVENT_NAME") == "workflow_run"
        and not os.environ.get("INPUT_PR_NUMBER")
    ):
        for _ in range(_ROLLUP_SETTLE_RETRIES):
            time.sleep(_ROLLUP_SETTLE_DELAY_SECONDS)
            refreshed = _fetch_pr(repo, pr_number)
            if isinstance(refreshed, dict):
                pr = refreshed
            ci_status = _ci_status(node_id, pr)
            if ci_status != "pending":
                break

    _emit(
        pr_number,
        has_verdict,
        verdict_value,
        stale,
        issue_key,
        ci_status,
        node_id,
        head_sha,
        escalated,
    )
    return 0


def _emit(
    pr_number: str,
    has_verdict: str,
    verdict: str,
    stale: str,
    issue_key: str,
    ci_status: str,
    node_id: str = "",
    head_sha: str = "",
    escalated: str = "false",
) -> None:
    set_output("pr_number", pr_number)
    set_output("has_verdict", has_verdict)
    set_output("verdict", verdict)
    set_output("stale", stale)
    set_output("issue_key", issue_key)
    set_output("ci_status", ci_status)
    set_output("node_id", node_id)
    set_output("head_sha", head_sha)
    set_output("escalated", escalated)
    print(
        f"pr={pr_number} has_verdict={has_verdict} verdict={verdict} "
        f"stale={stale} ci_status={ci_status}"
    )


if __name__ == "__main__":
    sys.exit(main())
