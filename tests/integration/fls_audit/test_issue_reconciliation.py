import copy
from collections.abc import Callable
from typing import Any

import pytest
import requests

from scripts import fls_audit_issue as audit
from tests.unit.fls_audit.test_issue_state import report_with_changes, spec_lock

BOT_USER = {"login": audit.ACTIONS_BOT_LOGIN, "id": audit.ACTIONS_BOT_ID, "type": "Bot"}


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issue_values: dict[int, dict[str, Any]] = {}
        self.comment_values: dict[int, list[dict[str, Any]]] = {}
        self.next_issue = 2000
        self.next_comment = 1
        self.label_exists = True
        self.mutations: list[tuple[str, int | None]] = []
        self.failures: dict[str, tuple[str, bool]] = {}
        self.stale_after_write: dict[str, int] = {}
        self.stale_issue_values: dict[int, list[dict[str, Any]]] = {}
        self.hidden_issue_reads: dict[int, int] = {}
        self.hidden_comment_reads = 0
        self.read_failures: dict[str, int] = {}
        self.read_failures_after_write: dict[str, int] = {}
        self.issue_read_hook: Callable[[dict[str, Any]], None] | None = None

    def fail(self, operation: str, timing: str, *, request_error: bool = False) -> None:
        self.failures[operation] = (timing, request_error)

    def maybe_fail(self, operation: str, timing: str) -> None:
        failure = self.failures.get(operation)
        if failure and failure[0] == timing:
            del self.failures[operation]
            if failure[1]:
                raise requests.ConnectionError(f"{operation} connection failed {timing} write")
            raise audit.AuditIssueError(f"{operation} failed {timing} write")

    def ensure_label(self, _label: str) -> None:
        if self.label_exists:
            return
        self.mutations.append(("label", None))
        self.maybe_fail("label", "before")
        self.label_exists = True
        self.maybe_fail("label", "after")

    def issues(self) -> list[dict[str, Any]]:
        if self.read_failures.get("issues", 0):
            self.read_failures["issues"] -= 1
            raise requests.ConnectionError("issues read failed")
        values = []
        for number, value in self.issue_values.items():
            if self.hidden_issue_reads.get(number, 0):
                self.hidden_issue_reads[number] -= 1
                continue
            stale = self.stale_issue_values.get(number, [])
            values.append(copy.deepcopy(stale.pop(0) if stale else value))
        return values

    def issue(self, number: int) -> dict[str, Any]:
        stale = self.stale_issue_values.get(number, [])
        if stale:
            return copy.deepcopy(stale.pop(0))
        if self.issue_read_hook is not None:
            hook = self.issue_read_hook
            self.issue_read_hook = None
            hook(self.issue_values[number])
        return copy.deepcopy(self.issue_values[number])

    def comments(self, number: int) -> list[dict[str, Any]]:
        if self.read_failures.get("comments", 0):
            self.read_failures["comments"] -= 1
            raise requests.ConnectionError("comments read failed")
        values = self.comment_values.get(number, [])
        if self.hidden_comment_reads and values:
            self.hidden_comment_reads -= 1
            values = values[:-1]
        return copy.deepcopy(values)

    def patch_issue(self, number: int, data: dict[str, Any]) -> dict[str, Any]:
        self.mutations.append(("patch", number))
        operation = "body_patch" if "body" in data else "state_patch" if "state" in data else "identity_patch"
        self.maybe_fail(operation, "before")
        value = self.issue_values[number]
        previous = copy.deepcopy(value)
        patch = copy.deepcopy(data)
        if "labels" in patch:
            patch["labels"] = [{"name": label} for label in patch["labels"]]
        value.update(patch)
        if data.get("state") == "open":
            value["state_reason"] = "reopened"
        stale_reads = self.stale_after_write.pop(operation, 0)
        self.stale_issue_values.setdefault(number, []).extend(copy.deepcopy(previous) for _ in range(stale_reads))
        self.maybe_fail(operation, "after")
        return copy.deepcopy(value)

    def create_issue(self, title: str, body: str, label: str) -> dict[str, Any]:
        number = self.next_issue
        self.mutations.append(("create", number))
        self.maybe_fail("create", "before")
        value = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label}],
            "state": "open",
            "user": copy.deepcopy(BOT_USER),
        }
        self.next_issue += 1
        self.issue_values[number] = value
        self.hidden_issue_reads[number] = self.stale_after_write.pop("create", 0)
        self.read_failures["issues"] = self.read_failures_after_write.pop("create", 0)
        self.maybe_fail("create", "after")
        return copy.deepcopy(value)

    def post_comment(self, number: int, body: str) -> dict[str, Any]:
        self.mutations.append(("comment", number))
        self.maybe_fail("comment", "before")
        value = {"id": self.next_comment, "body": body, "user": copy.deepcopy(BOT_USER)}
        self.next_comment += 1
        self.comment_values.setdefault(number, []).append(value)
        self.hidden_comment_reads = self.stale_after_write.pop("comment", 0)
        self.read_failures["comments"] = self.read_failures_after_write.pop("comment", 0)
        self.maybe_fail("comment", "after")
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

    assert reconcile(client, report) == "Reconciled audit issue #2000."
    assert client.mutations == [("create", 2000)]

    client.mutations.clear()
    assert reconcile(client, report) == "Audit issue #2000 is already current."
    assert client.mutations == []


@pytest.mark.integration
def test_changed_run_posts_one_comment_and_updates_state() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()

    current = report_with_changes(live_checksum="live-b")
    current["metadata"]["current_commit"] = "c" * 40
    assert reconcile(client, current) == "Reconciled audit issue #2000."

    number = next(iter(client.issue_values))
    assert client.mutations == [("comment", 2000), ("patch", 2000)]
    assert len(client.comment_values[number]) == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None
    assert state["sequence"] == 1


@pytest.mark.integration
def test_transition_body_overflow_fails_before_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number = next(iter(client.issue_values))
    current_size = len(client.issue_values[number]["body"].encode("utf-8"))
    monkeypatch.setattr(audit, "MAX_ISSUE_BODY_BYTES", current_size + 100)
    client.mutations.clear()

    with pytest.raises(audit.AuditIssueError, match="Compact issue body"):
        reconcile(client, report_with_changes(live_checksum="x" * 1_000))

    assert client.mutations == []
    assert client.comment_values.get(number, []) == []


@pytest.mark.integration
def test_ambiguous_comment_write_is_not_duplicated() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.mutations.clear()
    client.fail("comment", "after")

    reconcile(client, report_with_changes(live_checksum="live-b"))

    number = next(iter(client.issue_values))
    assert len(client.comment_values[number]) == 1
    assert [mutation[0] for mutation in client.mutations].count("comment") == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None and state["sequence"] == 1


@pytest.mark.integration
def test_create_failure_before_write_is_safe_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    client.fail("create", "before")
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    with pytest.raises(audit.AuditIssueError, match="create failed before write"):
        reconcile(client, report_with_changes())

    assert client.issue_values == {}
    reconcile(client, report_with_changes())
    assert len(client.issue_values) == 1


@pytest.mark.integration
def test_create_failure_after_write_recovers_created_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    client.fail("create", "after", request_error=True)
    client.stale_after_write["create"] = 2
    client.read_failures_after_write["create"] = 1
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    reconcile(client, report_with_changes())

    assert len(client.issue_values) == 1
    assert [mutation[0] for mutation in client.mutations].count("create") == 1


@pytest.mark.integration
def test_comment_failure_before_write_is_safe_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, initial = current_issue(client)
    client.fail("comment", "before")
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    with pytest.raises(audit.AuditIssueError, match="comment failed before write"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.comment_values.get(number, []) == []
    _, _, stale = current_issue(client)
    assert stale["sequence"] == initial["sequence"]

    reconcile(client, report_with_changes(live_checksum="live-b"))
    assert len(client.comment_values[number]) == 1


@pytest.mark.integration
def test_ambiguous_comment_polls_through_stale_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, _ = current_issue(client)
    client.fail("comment", "after", request_error=True)
    client.stale_after_write["comment"] = 2
    client.read_failures_after_write["comment"] = 1
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    reconcile(client, report_with_changes(live_checksum="live-b"))

    assert len(client.comment_values[number]) == 1


@pytest.mark.integration
@pytest.mark.parametrize("timing", ["before", "after"])
def test_label_failure_is_safe_to_retry(timing: str) -> None:
    client = FakeGitHubClient()
    client.label_exists = False
    client.fail("label", timing, request_error=True)

    with pytest.raises(requests.ConnectionError, match=f"label connection failed {timing} write"):
        reconcile(client, report_with_changes())

    assert client.issue_values == {}
    reconcile(client, report_with_changes())
    assert client.label_exists
    assert len(client.issue_values) == 1


@pytest.mark.integration
def test_comment_is_recovered_after_body_patch_failure() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, initial = current_issue(client)
    client.fail("body_patch", "before")

    with pytest.raises(audit.AuditIssueError, match="body_patch failed before write"):
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
def test_body_patch_failure_after_write_is_idempotent_on_retry() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, _ = current_issue(client)
    client.fail("body_patch", "after")

    with pytest.raises(audit.AuditIssueError, match="body_patch failed after write"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    assert len(client.comment_values[number]) == 1
    _, _, written = current_issue(client)
    assert written["sequence"] == 1
    client.mutations.clear()

    reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.mutations == []
    assert len(client.comment_values[number]) == 1


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
            "body": audit.batch_marker(
                state["campaign"], 1, value, target, state["applied"]["semantic_digest"]
            ),
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
@pytest.mark.parametrize("timing", ["before", "after"])
def test_identity_patch_failure_is_safe_to_retry(timing: str) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, issue, _ = current_issue(client)
    issue["title"] = "damaged title"
    issue["labels"] = []
    client.fail("identity_patch", timing)

    with pytest.raises(audit.AuditIssueError, match=f"identity_patch failed {timing} write"):
        reconcile(client, report_with_changes())

    reconcile(client, report_with_changes())
    assert client.issue_values[number]["title"].startswith("FLS audit: spec.lock drift")
    assert client.issue_values[number]["labels"] == [{"name": "fls-audit"}]


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

    assert reconcile(client, clean) == "Reconciled audit issue #2000."

    number = next(iter(client.issue_values))
    assert client.mutations == [("comment", 2000), ("patch", 2000), ("patch", 2000)]
    assert client.issue_values[number]["state"] == "closed"
    assert len(client.comment_values[number]) == 1
    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None and state["applied"]["items"] == {}


@pytest.mark.integration
@pytest.mark.parametrize("timing", ["before", "after"])
def test_close_failure_is_idempotent_on_retry(timing: str) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, _ = current_issue(client)
    clean = report_with_changes()
    clean["changes"] = {"added": [], "removed": [], "changed": []}
    clean["affected_guidelines"] = {}
    clean["summary"] = dict.fromkeys(clean["summary"], 0)
    clean["text"] = {"added": {}, "removed": {}, "content_diffs": []}
    client.fail("state_patch", timing)

    with pytest.raises(audit.AuditIssueError, match=f"state_patch failed {timing} write"):
        reconcile(client, clean)

    assert len(client.comment_values[number]) == 1
    client.mutations.clear()
    reconcile(client, clean)
    assert len(client.comment_values[number]) == 1
    assert client.issue_values[number]["state"] == "closed"
    assert [mutation[0] for mutation in client.mutations] == (["patch"] if timing == "before" else [])


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

    assert reconcile(client, report) == f"Reconciled audit issue #{audit.LEGACY_ISSUE_NUMBER}."

    assert set(client.issue_values) == {audit.LEGACY_ISSUE_NUMBER}
    assert client.mutations == [("patch", audit.LEGACY_ISSUE_NUMBER)]
    assert client.issue_values[audit.LEGACY_ISSUE_NUMBER]["title"].startswith("FLS audit: spec.lock drift")
    assert "Pre-campaign audit body" in client.issue_values[audit.LEGACY_ISSUE_NUMBER]["body"]
    assert audit.parse_state(client.issue_values[audit.LEGACY_ISSUE_NUMBER]["body"]) is not None


@pytest.mark.integration
def test_pre_campaign_issue_with_multiple_baselines_fails_closed() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    baseline = report["metadata"]["baseline_commit"]
    client.issue_values[audit.LEGACY_ISSUE_NUMBER] = {
        "number": audit.LEGACY_ISSUE_NUMBER,
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

    with pytest.raises(audit.AuditIssueError, match="exactly one baseline commit"):
        reconcile(client, report)

    assert client.mutations == []


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
def test_human_text_added_during_transition_is_preserved() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, _ = current_issue(client)
    client.issue_read_hook = lambda issue: issue.update(body=f"Maintainer note\n\n{issue['body']}")

    reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.issue_values[number]["body"].startswith("Maintainer note\n\n")


@pytest.mark.integration
def test_concurrent_state_change_is_not_overwritten() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, _, _ = current_issue(client)

    def change_state(issue: dict[str, Any]) -> None:
        state = audit.parse_state(issue["body"])
        assert state is not None
        state["applied"]["body_digest"] = f"sha256:{'f' * 64}"
        issue["body"] = audit.STATE_RE.sub(audit.state_marker(state), issue["body"])

    client.issue_read_hook = change_state

    with pytest.raises(audit.AuditIssueError, match="changed concurrently"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    state = audit.parse_state(client.issue_values[number]["body"])
    assert state is not None and state["sequence"] == 0


@pytest.mark.integration
def test_runtime_verification_retries_stale_issue_read(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.stale_after_write["body_patch"] = 1
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    reconcile(client, report_with_changes(live_checksum="live-b"))

    _, _, state = current_issue(client)
    assert state["sequence"] == 1


@pytest.mark.integration
def test_runtime_verification_retries_issue_and_comment_staleness_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    client.stale_after_write["body_patch"] = 1
    client.stale_after_write["comment"] = 2
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    reconcile(client, report_with_changes(live_checksum="live-b"))

    _, _, state = current_issue(client)
    assert state["sequence"] == 1


@pytest.mark.integration
def test_posted_comment_state_is_recovered_on_next_run() -> None:
    client = FakeGitHubClient()
    first = report_with_changes()
    second = report_with_changes(live_checksum="live-b")
    reconcile(client, first)
    number, issue, state = current_issue(client)
    target = audit.make_applied(second, "# Report\n", audit.canonical_items(second))
    value = audit.batch_id(state["campaign"], 1, state["applied"]["semantic_digest"], target["semantic_digest"])
    marker = audit.batch_marker(state["campaign"], 1, value, target, state["applied"]["semantic_digest"])
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
    marker = audit.batch_marker(
        state["campaign"], 1, value, missed_target, state["applied"]["semantic_digest"]
    )
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
@pytest.mark.parametrize("timing", ["before", "after"])
def test_reopen_failure_is_safe_to_retry(timing: str) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, issue, _ = current_issue(client)
    issue["state"] = "closed"
    client.fail("state_patch", timing)

    with pytest.raises(audit.AuditIssueError, match=f"state_patch failed {timing} write"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.comment_values.get(number, []) == []
    reconcile(client, report_with_changes(live_checksum="live-b"))
    assert client.issue_values[number]["state"] == "open"
    assert len(client.comment_values[number]) == 1


@pytest.mark.integration
def test_new_lock_campaign_creates_new_issue_and_closes_old() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    reconcile(client, report)
    old_number, _, _ = current_issue(client)
    new_lock = {"documents": [{"link": "two.html", "sections": [{"id": "fls_two"}]}]}
    client.mutations.clear()

    assert (
        audit.reconcile(client, report, "# Report\n", new_lock, "fls-audit", "FLS audit:")
        == "Reconciled audit issue #2001."
    )

    assert len(client.issue_values) == 2
    assert client.mutations == [("create", 2001), ("comment", 2000), ("patch", 2000)]
    assert client.issue_values[old_number]["state"] == "closed"
    assert len(client.comment_values[old_number]) == 1


@pytest.mark.integration
def test_superseded_campaign_close_failure_does_not_duplicate_comment() -> None:
    client = FakeGitHubClient()
    report = report_with_changes()
    reconcile(client, report)
    old_number, _, _ = current_issue(client)
    new_lock = {"documents": [{"link": "two.html", "sections": [{"id": "fls_two"}]}]}
    client.fail("state_patch", "before")

    with pytest.raises(audit.AuditIssueError, match="state_patch failed before write"):
        audit.reconcile(client, report, "# Report\n", new_lock, "fls-audit", "FLS audit:")

    assert len(client.comment_values[old_number]) == 1
    audit.reconcile(client, report, "# Report\n", new_lock, "fls-audit", "FLS audit:")
    assert len(client.comment_values[old_number]) == 1
    assert client.issue_values[old_number]["state"] == "closed"


@pytest.mark.integration
def test_duplicate_bot_batch_marker_fails_without_mutation() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    reconcile(client, report_with_changes(live_checksum="live-b"))
    number, _, _ = current_issue(client)
    duplicate = copy.deepcopy(client.comment_values[number][0])
    duplicate["id"] = 999
    client.comment_values[number].append(duplicate)
    client.mutations.clear()

    with pytest.raises(audit.AuditIssueError, match="duplicate bot batch markers"):
        reconcile(client, report_with_changes(live_checksum="live-b"))

    assert client.mutations == []


@pytest.mark.integration
def test_runtime_verification_detects_deleted_transition_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    reconcile(client, report_with_changes(live_checksum="live-b"))
    number, _, _ = current_issue(client)
    client.comment_values[number] = []
    monkeypatch.setattr(audit.time, "sleep", lambda _delay: None)

    with pytest.raises(audit.AuditIssueError, match="comment history does not match"):
        reconcile(client, report_with_changes(live_checksum="live-b"))


@pytest.mark.integration
def test_duplicate_campaign_issues_fail_without_mutation() -> None:
    client = FakeGitHubClient()
    reconcile(client, report_with_changes())
    number, issue, _ = current_issue(client)
    duplicate = copy.deepcopy(issue)
    duplicate["number"] = number + 1
    client.issue_values[number + 1] = duplicate
    client.mutations.clear()

    with pytest.raises(audit.AuditIssueError, match="Multiple FLS audit issues claim campaign"):
        reconcile(client, report_with_changes())

    assert client.mutations == []


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
