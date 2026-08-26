import copy
from typing import Any

import pytest

from scripts import fls_audit_issue as audit
from tests.unit.fls_audit.test_issue_state import report_with_changes, spec_lock

BOT_USER = {"login": audit.ACTIONS_BOT_LOGIN, "id": audit.ACTIONS_BOT_ID, "type": "Bot"}


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issue_values: dict[int, dict[str, Any]] = {}
        self.comment_values: dict[int, list[dict[str, Any]]] = {}
        self.next_issue = 2000
        self.next_comment = 1
        self.mutations: list[tuple[str, int | None]] = []
        self.fail_comment_after_write = False
        self.fail_body_patch_before_write = False

    def ensure_label(self, _label: str) -> None:
        return

    def issues(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(value) for value in self.issue_values.values()]

    def issue(self, number: int) -> dict[str, Any]:
        return copy.deepcopy(self.issue_values[number])

    def comments(self, number: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self.comment_values.get(number, []))

    def patch_issue(self, number: int, data: dict[str, Any]) -> dict[str, Any]:
        self.mutations.append(("patch", number))
        if "body" in data and self.fail_body_patch_before_write:
            self.fail_body_patch_before_write = False
            raise audit.AuditIssueError("body patch failed")
        value = self.issue_values[number]
        value.update(copy.deepcopy(data))
        if data.get("state") == "open":
            value["state_reason"] = "reopened"
        return copy.deepcopy(value)

    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]:
        number = self.next_issue
        self.next_issue += 1
        value = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label}],
            "state": "open",
            "user": copy.deepcopy(BOT_USER),
        }
        self.issue_values[number] = value
        self.mutations.append(("create", number))
        return copy.deepcopy(value)

    def post_comment(self, number: int, body: str) -> dict[str, Any]:
        value = {"id": self.next_comment, "body": body, "user": copy.deepcopy(BOT_USER)}
        self.next_comment += 1
        self.comment_values.setdefault(number, []).append(value)
        self.mutations.append(("comment", number))
        if self.fail_comment_after_write:
            self.fail_comment_after_write = False
            raise audit.AuditIssueError("ambiguous comment failure")
        return copy.deepcopy(value)


def reconcile(client: FakeGitHubClient, report: dict) -> str:
    return audit.reconcile(client, report, "# Report\n", spec_lock(), "fls-audit", "FLS audit:")


def current_issue(client: FakeGitHubClient) -> tuple[int, dict[str, Any], dict[str, Any]]:
    number = max(client.issue_values)
    issue = client.issue_values[number]
    state = audit.parse_state(issue["body"])
    assert state is not None
    return number, issue, state


@pytest.mark.integration
def test_create_then_identical_run_performs_no_writes() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()

    reconcile(client, report)
    assert [mutation[0] for mutation in client.mutations] == ["create"]

    client.mutations.clear()
    reconcile(client, report)
    assert client.mutations == []


@pytest.mark.integration
def test_changed_run_posts_one_comment_and_updates_state() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()

    current = report_with_changes(live_checksum="live-b")
    current["metadata"]["current_commit"] = "c" * 40
    reconcile(client, current)

    number = next(iter(client.issue_values))
    assert [mutation[0] for mutation in client.mutations] == ["comment", "patch"]
    assert len(client.comment_values[number]) == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None
    assert state["sequence"] == 1


@pytest.mark.integration
def test_ambiguous_comment_write_is_not_duplicated() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()
    client.fail_comment_after_write = True

    reconcile(client, report_with_changes(live_checksum="live-b"))

    number = next(iter(client.issue_values))
    assert len(client.comment_values[number]) == 1
    assert [mutation[0] for mutation in client.mutations].count("comment") == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None and state["sequence"] == 1


@pytest.mark.integration
def test_comment_is_recovered_after_body_patch_failure() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, initial = current_issue(client)
    client.fail_body_patch_before_write = True

    with pytest.raises(audit.AuditIssueError, match="body patch failed"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    assert len(client.comment_values[number]) == 1
    _, _, stale = current_issue(client)
    assert stale["sequence"] == initial["sequence"] == 0
    client.mutations.clear()

    reconcile(client, report_with_changes(live_checksum="live-b"))

    assert [mutation[0] for mutation in client.mutations] == ["patch"]
    assert len(client.comment_values[number]) == 1
    _, _, recovered = current_issue(client)
    assert recovered["sequence"] == 1


@pytest.mark.integration
def test_untrusted_comment_marker_cannot_advance_state() -> None:
    client = FakeGitHubClient()
    first = report_with_changes()
    second = report_with_changes(live_checksum="live-b")
    reconcile(client, first)
    number, _, state = current_issue(client)
    target = audit.make_applied(second, "# Report\n", audit.canonical_items(second))
    value = audit.batch_id(state["campaign"], 1, state["applied"]["semantic_digest"], target["semantic_digest"])
    client.comment_values[number] = [
        {
            "id": 1,
            "body": audit.batch_marker(state["campaign"], 1, value, target),
            "user": {"login": "contributor", "id": 7, "type": "User"},
        }
    ]
    client.mutations.clear()

    reconcile(client, second)

    assert [mutation[0] for mutation in client.mutations] == ["comment", "patch"]
    assert len(client.comment_values[number]) == 2
    _, _, updated = current_issue(client)
    assert updated["sequence"] == 1


@pytest.mark.integration
def test_untrusted_issue_marker_cannot_claim_campaign() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    current = audit.make_applied(report, "# Report\n", audit.canonical_items(report))
    state = audit.make_state(audit.campaign_id(spec_lock()), 0, current)
    client.issue_values[100] = {
        "number": 100,
        "title": audit.expected_title("FLS audit:", state["campaign"]),
        "body": audit.managed_body("", report, "# Report\n", state, ""),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": {"login": "contributor", "id": 7, "type": "User"},
    }

    reconcile(client, report)

    assert set(client.issue_values) == {100, 2000}
    assert client.issue_values[100]["body"] == audit.managed_body("", report, "# Report\n", state, "")


@pytest.mark.integration
def test_clean_run_comments_and_closes_campaign() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()
    clean = report_with_changes()
    clean["changes"] = {"added": [], "removed": [], "changed": []}
    clean["affected_guidelines"] = {}
    clean["summary"] = dict.fromkeys(clean["summary"], 0)
    clean["text"] = {"added": {}, "removed": {}, "content_diffs": []}

    reconcile(client, clean)

    number = next(iter(client.issue_values))
    assert client.issue_values[number]["state"] == "closed"
    assert len(client.comment_values[number]) == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None and state["applied"]["items"] == {}


@pytest.mark.integration
def test_matching_legacy_issue_is_adopted_without_new_issue() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    client.issue_values[audit.LEGACY_ISSUE_NUMBER] = {
        "number": audit.LEGACY_ISSUE_NUMBER,
        "title": "FLS audit: changes detected (2026-08-27)",
        "body": (
            "## What to do\nlegacy\n\n# FLS Spec Lock Audit Report\n\n"
            f"- Baseline commit: `{report['metadata']['baseline_commit']}`\n"
        ),
        "labels": [{"name": "fls-audit"}],
        "state": "open",
        "user": copy.deepcopy(BOT_USER),
    }

    reconcile(client, report)

    assert set(client.issue_values) == {audit.LEGACY_ISSUE_NUMBER}
    assert client.issue_values[audit.LEGACY_ISSUE_NUMBER]["title"].startswith("FLS audit: spec.lock drift")
    assert "Legacy pre-campaign audit body" in client.issue_values[audit.LEGACY_ISSUE_NUMBER]["body"]
    assert audit.parse_state(client.issue_values[audit.LEGACY_ISSUE_NUMBER]["body"]) is not None


@pytest.mark.integration
def test_body_only_change_does_not_comment() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()

    reconcile(client, report_with_changes(affected=False))

    assert [mutation[0] for mutation in client.mutations] == ["patch"]
    number, _, _ = current_issue(client)
    assert client.comment_values.get(number, []) == []


@pytest.mark.integration
def test_damaged_managed_report_is_repaired_without_comment() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    reconcile(client, report)
    number, issue, _ = current_issue(client)
    issue["body"] = issue["body"].replace("# Report", "# Damaged report")
    client.mutations.clear()

    reconcile(client, report)

    assert [mutation[0] for mutation in client.mutations] == ["patch"]
    assert "# Damaged report" not in client.issue_values[number]["body"]
    assert client.comment_values.get(number, []) == []


@pytest.mark.integration
def test_posted_comment_state_is_recovered_on_next_run() -> None:
    client = FakeGitHubClient()
    first = report_with_changes()
    second = report_with_changes(live_checksum="live-b")
    reconcile(client, first)
    number, issue, state = current_issue(client)
    target = audit.make_applied(second, "# Report\n", audit.canonical_items(second))
    value = audit.batch_id(state["campaign"], 1, state["applied"]["semantic_digest"], target["semantic_digest"])
    marker = audit.batch_marker(state["campaign"], 1, value, target)
    client.comment_values[number] = [{"id": 1, "body": marker, "user": copy.deepcopy(BOT_USER)}]
    client.mutations.clear()

    reconcile(client, second)

    _, _, recovered = current_issue(client)
    assert recovered["sequence"] == 1
    assert len(client.comment_values[number]) == 1


@pytest.mark.integration
def test_posted_comment_state_is_used_as_base_for_later_catch_up() -> None:
    client = FakeGitHubClient()
    first = report_with_changes()
    missed = report_with_changes(live_checksum="live-b")
    latest = report_with_changes(live_checksum="live-c")
    reconcile(client, first)
    number, issue, state = current_issue(client)
    missed_target = audit.make_applied(missed, "# Report\n", audit.canonical_items(missed))
    value = audit.batch_id(state["campaign"], 1, state["applied"]["semantic_digest"], missed_target["semantic_digest"])
    marker = audit.batch_marker(state["campaign"], 1, value, missed_target)
    client.comment_values[number] = [{"id": 1, "body": marker, "user": copy.deepcopy(BOT_USER)}]
    client.mutations.clear()

    reconcile(client, latest)

    _, _, recovered = current_issue(client)
    latest_target = audit.make_applied(latest, "# Report\n", audit.canonical_items(latest))
    assert recovered["applied"]["semantic_digest"] == latest_target["semantic_digest"]
    assert recovered["sequence"] == 2
    assert len(client.comment_values[number]) == 2


@pytest.mark.integration
def test_closed_stale_campaign_reopens() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, issue, _ = current_issue(client)
    issue["state"] = "closed"
    client.mutations.clear()

    reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.issue_values[number]["state"] == "open"
    assert ("patch", number) in client.mutations
    assert len(client.comment_values[number]) == 1


@pytest.mark.integration
def test_new_lock_campaign_creates_new_issue_and_closes_old() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    reconcile(client, report)
    old_number, _, _ = current_issue(client)
    new_lock = {"documents": [{"link": "two.html", "sections": [{"id": "fls_two"}]}]}
    client.mutations.clear()

    audit.reconcile(client, report, "# Report\n", new_lock, "fls-audit", "FLS audit:")

    assert len(client.issue_values) == 2
    assert client.issue_values[old_number]["state"] == "closed"
    assert len(client.comment_values[old_number]) == 1


@pytest.mark.integration
def test_open_legacy_issue_with_old_baseline_is_closed_after_new_campaign_creation() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    client.issue_values[audit.LEGACY_ISSUE_NUMBER] = {
        "number": audit.LEGACY_ISSUE_NUMBER,
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
    assert client.issue_values[audit.LEGACY_ISSUE_NUMBER]["state"] == "closed"
    assert len(client.comment_values[audit.LEGACY_ISSUE_NUMBER]) == 1


@pytest.mark.integration
def test_clean_report_closes_matching_legacy_issue_without_replacement() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    report["changes"] = {"added": [], "removed": [], "changed": []}
    report["affected_guidelines"] = {}
    report["summary"] = dict.fromkeys(report["summary"], 0)
    report["text"] = {"added": {}, "removed": {}, "content_diffs": []}
    client.issue_values[audit.LEGACY_ISSUE_NUMBER] = {
        "number": audit.LEGACY_ISSUE_NUMBER,
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

    assert set(client.issue_values) == {audit.LEGACY_ISSUE_NUMBER}
    assert client.issue_values[audit.LEGACY_ISSUE_NUMBER]["state"] == "closed"
    assert len(client.comment_values[audit.LEGACY_ISSUE_NUMBER]) == 1
