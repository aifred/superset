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

"""Tests for review_reconcile PR resolution and CI-status aggregation."""

import importlib
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

review_reconcile = importlib.import_module("review_reconcile")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "INPUT_PR_NUMBER",
        "EVENT_NAME",
        "IS_PULL_REQUEST",
        "ISSUE_NUMBER",
        "WF_PULL_REQUESTS",
        "WF_HEAD_SHA",
    ):
        monkeypatch.delenv(var, raising=False)


def test_explicit_input_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sweep path passes the PR directly; it must win over event resolution.
    monkeypatch.setenv("INPUT_PR_NUMBER", "84")
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("IS_PULL_REQUEST", "true")
    monkeypatch.setenv("ISSUE_NUMBER", "999")
    assert review_reconcile._resolve_pr_number("o/r") == "84"


def test_explicit_input_whitespace_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INPUT_PR_NUMBER", "  ")
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("IS_PULL_REQUEST", "true")
    monkeypatch.setenv("ISSUE_NUMBER", "12")
    assert review_reconcile._resolve_pr_number("o/r") == "12"


def test_issue_comment_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("IS_PULL_REQUEST", "true")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    assert review_reconcile._resolve_pr_number("o/r") == "42"


def test_issue_comment_non_pr_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("IS_PULL_REQUEST", "false")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    assert review_reconcile._resolve_pr_number("o/r") == ""


def test_workflow_run_resolution_from_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENT_NAME", "workflow_run")
    monkeypatch.setenv("WF_PULL_REQUESTS", '[{"number": 7}]')
    assert review_reconcile._resolve_pr_number("o/r") == "7"


def _rollup(pr: list[dict[str, Any]]) -> dict[str, Any]:
    return {"statusCheckRollup": pr}


def test_ci_status_all_success_excludes_devin_review() -> None:
    pr = _rollup(
        [
            {"name": "E2E", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "devin-review", "state": "PENDING"},
        ]
    )
    assert review_reconcile._ci_status("", pr) == "success"


def test_ci_status_pending_when_a_check_incomplete() -> None:
    pr = _rollup(
        [
            {"name": "E2E", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Playwright", "status": "IN_PROGRESS", "conclusion": None},
        ]
    )
    assert review_reconcile._ci_status("", pr) == "pending"


def test_ci_status_failure_short_circuits() -> None:
    pr = _rollup(
        [
            {"name": "E2E", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    )
    assert review_reconcile._ci_status("", pr) == "failure"


def test_ci_status_no_checks_is_pending() -> None:
    assert review_reconcile._ci_status("", _rollup([])) == "pending"
