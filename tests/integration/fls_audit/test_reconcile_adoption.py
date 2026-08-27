import copy

import pytest

from scripts.fls_audit_issue_lib import reconcile as reconciliation
from scripts.fls_audit_issue_lib.errors import AuditIssueError
from scripts.fls_audit_issue_lib.state import parse_state
from tests.fls_audit_fixtures import report_with_changes, spec_lock
from tests.integration.fls_audit.fake_github import BOT_USER, FakeGitHubClient


def reconcile(client: FakeGitHubClient, report: dict) -> str:
    return reconciliation.reconcile(client, report, "# Report\n", spec_lock(), "fls-audit", "FLS audit:")


@pytest.mark.integration
def test_matching_legacy_issue_is_adopted_without_new_issue() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER] = {
        "number": reconciliation.LEGACY_ISSUE_NUMBER,
        "title": "FLS audit: changes detected (2026-08-27)",
        "body": (
            "## What to do\nlegacy\n\n# FLS Spec Lock Audit Report\n\n"
            f"- Baseline commit: `{report['metadata']['baseline_commit']}`\n"
        ),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": copy.deepcopy(BOT_USER),
    }

    assert reconcile(client, report) == f"Reconciled audit issue #{reconciliation.LEGACY_ISSUE_NUMBER}."

    assert set(client.issue_values) == {reconciliation.LEGACY_ISSUE_NUMBER}
    assert client.mutations == [("patch", reconciliation.LEGACY_ISSUE_NUMBER)]
    assert client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER]["title"].startswith(
        "FLS audit: spec.lock drift"
    )
    assert "Pre-campaign audit body" in client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER]["body"]
    assert parse_state(client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER]["body"]) is not None


@pytest.mark.integration
def test_pre_campaign_issue_with_multiple_baselines_fails_closed() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    baseline = report["metadata"]["baseline_commit"]
    client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER] = {
        "number": reconciliation.LEGACY_ISSUE_NUMBER,
        "title": "FLS audit: changes detected (2026-08-27)",
        "body": (
            "## What to do\nlegacy\n\n# FLS Spec Lock Audit Report\n\n"
            f"- Baseline commit: `{baseline}`\n"
            f"- Baseline commit: `{baseline}`\n"
        ),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": copy.deepcopy(BOT_USER),
    }

    with pytest.raises(AuditIssueError, match="exactly one baseline commit"):
        reconcile(client, report)

    assert client.mutations == []


@pytest.mark.integration
def test_open_legacy_issue_with_old_baseline_is_closed_after_new_campaign_creation() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER] = {
        "number": reconciliation.LEGACY_ISSUE_NUMBER,
        "title": "FLS audit: changes detected (2026-08-01)",
        "body": (
            "## What to do\nlegacy\n\n# FLS Spec Lock Audit Report\n\n"
            f"- Baseline commit: `{'d' * 40}`\n"
        ),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": copy.deepcopy(BOT_USER),
    }

    reconcile(client, report)

    assert len(client.issue_values) == 2
    assert client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER]["state"] == "closed"
    assert len(client.comment_values[reconciliation.LEGACY_ISSUE_NUMBER]) == 1


@pytest.mark.integration
def test_clean_report_closes_matching_legacy_issue_without_replacement() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    report["changes"] = {"added": [], "removed": [], "changed": []}
    report["affected_guidelines"] = {}
    report["summary"] = dict.fromkeys(report["summary"], 0)
    report["text"] = {"added": {}, "removed": {}, "content_diffs": []}
    client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER] = {
        "number": reconciliation.LEGACY_ISSUE_NUMBER,
        "title": "FLS audit: changes detected (2026-08-01)",
        "body": (
            "## What to do\nlegacy\n\n# FLS Spec Lock Audit Report\n\n"
            f"- Baseline commit: `{report['metadata']['baseline_commit']}`\n"
        ),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": copy.deepcopy(BOT_USER),
    }

    reconcile(client, report)

    assert set(client.issue_values) == {reconciliation.LEGACY_ISSUE_NUMBER}
    assert client.issue_values[reconciliation.LEGACY_ISSUE_NUMBER]["state"] == "closed"
    assert len(client.comment_values[reconciliation.LEGACY_ISSUE_NUMBER]) == 1
