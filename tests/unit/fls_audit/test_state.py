import copy
import hashlib

import pytest

from scripts.fls_audit_issue_lib import reconcile, state
from scripts.fls_audit_issue_lib.errors import AuditIssueError
from tests.fls_audit_fixtures import report_with_changes, spec_lock


def test_campaign_ignores_metadata_and_json_formatting() -> None:
    first = spec_lock()
    second = copy.deepcopy(first)
    second["metadata"] = {"ignored": False, "new": "value"}

    assert state.campaign_id(first) == state.campaign_id(second)


def test_canonical_items_and_net_delta() -> None:
    previous_report = report_with_changes(live_checksum="live-a")
    current_report = report_with_changes(live_checksum="live-b")
    current_report["changes"]["removed"] = [
        {
            "fls_id": "fls_removed",
            "locked": {"checksum": "gone", "section_id": "3:1"},
        }
    ]
    previous = state.canonical_items(previous_report)
    current = state.canonical_items(current_report)

    new, updated, resolved = state.diff_items(previous, current)

    assert new == ["paragraph:fls_removed"]
    assert updated == ["paragraph:fls_added"]
    assert resolved == []


def test_body_digest_ignores_volatile_metadata_but_tracks_impact() -> None:
    first = report_with_changes()
    second = copy.deepcopy(first)
    second["metadata"]["generated_at"] = "later"
    second["metadata"]["spec_lock"] = "/different/path"

    markdown = "# Report\n\n- Generated: first\n- Spec lock: `/first/path`\n\nStable\n"
    other_markdown = "# Report\n\n- Generated: second\n- Spec lock: `/other/path`\n\nStable\n"
    assert state.report_body_digest(first, markdown) == state.report_body_digest(second, other_markdown)

    second["affected_guidelines"] = {}
    assert state.report_body_digest(first, markdown) != state.report_body_digest(second, other_markdown)
    assert state.report_body_digest(first, markdown) != state.report_body_digest(first, f"{markdown}Changed\n")


def test_state_marker_characterization() -> None:
    report = report_with_changes()
    applied = state.make_applied(report, "# Current report\n", state.canonical_items(report))
    issue_state = state.make_state(state.campaign_id(spec_lock()), 0, applied)

    assert hashlib.sha256(state.state_marker(issue_state).encode()).hexdigest() == (
        "33fc5cbda9fd109c9c8330b0b15afe26fc7a08348d4e83d997bfba0d8a85a1dd"
    )


def test_state_requires_consistent_schema_and_managed_region() -> None:
    report = report_with_changes()
    applied = state.make_applied(report, "# Report\n", state.canonical_items(report))
    issue_state = state.make_state(state.campaign_id(spec_lock()), 0, applied)

    damaged = copy.deepcopy(issue_state)
    damaged["sequence"] = True
    with pytest.raises(AuditIssueError, match="invalid campaign or sequence"):
        state.validate_state(damaged)

    damaged = copy.deepcopy(issue_state)
    damaged["applied"]["items"] = {}
    with pytest.raises(AuditIssueError, match="does not match"):
        state.validate_state(damaged)

    damaged = copy.deepcopy(issue_state)
    damaged["origin_semantic_digest"] = "sha256:" + "0" * 64
    with pytest.raises(AuditIssueError, match="sequence-zero state"):
        state.validate_state(damaged)

    with pytest.raises(AuditIssueError, match="outside its managed region"):
        state.parse_state(state.state_marker(issue_state))

    reversed_region = f"{state.MANAGED_END}\n{state.state_marker(issue_state)}\n{state.MANAGED_START}"
    with pytest.raises(AuditIssueError, match="boundaries are reversed"):
        state.parse_state(reversed_region)


def test_comment_recovery_rejects_sequence_gap() -> None:
    report = report_with_changes()
    current_report = report_with_changes(live_checksum="new")
    previous = state.make_applied(report, "# Report\n", state.canonical_items(report))
    current = state.make_applied(current_report, "# Report\n", state.canonical_items(current_report))
    campaign = state.campaign_id(spec_lock())
    issue_state = state.make_state(campaign, 0, previous)
    value = state.batch_id(campaign, 2, previous["semantic_digest"], current["semantic_digest"])
    comment = {
        "body": state.batch_marker(campaign, 2, value, current, previous["semantic_digest"]),
        "user": {
            "login": reconcile.ACTIONS_BOT_LOGIN,
            "id": reconcile.ACTIONS_BOT_ID,
            "type": "Bot",
        },
    }

    with pytest.raises(AuditIssueError, match="sequence jumps"):
        reconcile.recover_from_comments(issue_state, [comment])


def test_first_transition_is_grounded_in_campaign_origin() -> None:
    report = report_with_changes()
    current_report = report_with_changes(live_checksum="new")
    origin = state.make_applied(report, "# Report\n", state.canonical_items(report))
    current = state.make_applied(current_report, "# Report\n", state.canonical_items(current_report))
    campaign = state.campaign_id(spec_lock())
    issue_state = state.make_state(campaign, 1, current, origin["semantic_digest"])
    wrong_previous = "sha256:" + "0" * 64
    value = state.batch_id(campaign, 1, wrong_previous, current["semantic_digest"])
    comment = {
        "body": state.batch_marker(campaign, 1, value, current, wrong_previous),
        "user": {
            "login": reconcile.ACTIONS_BOT_LOGIN,
            "id": reconcile.ACTIONS_BOT_ID,
            "type": "Bot",
        },
    }

    with pytest.raises(AuditIssueError, match="invalid historical batch chain"):
        reconcile.verify_comment_history(issue_state, [comment])
