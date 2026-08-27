import copy
import hashlib

import pytest

from scripts import fls_audit_issue as audit


def report_with_changes(*, live_checksum: str = "live-a", affected: bool = True) -> dict:
    return {
        "metadata": {
            "baseline_commit": "a" * 40,
            "current_commit": "b" * 40,
            "generated_at": "2026-08-27T00:00:00+00:00",
            "spec_lock": "/tmp/src/spec.lock",
        },
        "changes": {
            "added": [
                {
                    "fls_id": "fls_added",
                    "live": {"checksum": live_checksum, "section_id": "1:1"},
                }
            ],
            "removed": [],
            "changed": [
                {
                    "fls_id": "fls_changed",
                    "locked": {"checksum": "old", "section_id": "2:1"},
                    "live": {"checksum": "new", "section_id": "2:1"},
                    "content_changed": True,
                    "section_changed": False,
                }
            ],
        },
        "affected_guidelines": (
            {"gui_example": {"title": "Example guideline", "changes": [{"fls_id": "fls_changed"}]}}
            if affected
            else {}
        ),
        "header_changes": [],
        "new_paragraph_assessments": [],
        "relevance": [],
        "section_reorders": [],
        "summary": {
            "added": 1,
            "removed": 0,
            "content_changed": 1,
            "renumbered_only": 0,
            "header_changed": 0,
            "section_reordered": 0,
            "section_changed": 0,
            "affected_guidelines": int(affected),
        },
        "text": {
            "added": {"fls_added": "Added paragraph."},
            "removed": {},
            "content_diffs": [
                {
                    "fls_id": "fls_changed",
                    "diff": ["--- before", "+++ after", "-old", "+new"],
                }
            ],
        },
    }


def spec_lock() -> dict:
    return {"documents": [{"link": "one.html", "sections": [{"id": "fls_one"}]}], "metadata": {"ignored": True}}


def test_campaign_ignores_metadata_and_json_formatting() -> None:
    first = spec_lock()
    second = copy.deepcopy(first)
    second["metadata"] = {"ignored": False, "new": "value"}

    assert audit.campaign_id(first) == audit.campaign_id(second)


def test_canonical_items_and_net_delta() -> None:
    previous_report = report_with_changes(live_checksum="live-a")
    current_report = report_with_changes(live_checksum="live-b")
    current_report["changes"]["removed"] = [
        {
            "fls_id": "fls_removed",
            "locked": {"checksum": "gone", "section_id": "3:1"},
        }
    ]
    previous = audit.canonical_items(previous_report)
    current = audit.canonical_items(current_report)

    new, updated, resolved = audit.diff_items(previous, current)

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
    assert audit.report_body_digest(first, markdown) == audit.report_body_digest(second, other_markdown)

    second["affected_guidelines"] = {}
    assert audit.report_body_digest(first, markdown) != audit.report_body_digest(second, other_markdown)

    assert audit.report_body_digest(first, markdown) != audit.report_body_digest(first, f"{markdown}Changed\n")


def test_managed_body_preserves_human_text() -> None:
    report = report_with_changes()
    applied = audit.make_applied(report, "# Current report\n", audit.canonical_items(report))
    state = audit.make_state(audit.campaign_id(spec_lock()), 0, applied)
    original = f"Human preface\n{audit.MANAGED_START}\nold\n{audit.state_marker(state)}\n{audit.MANAGED_END}\nHuman footer"

    updated = audit.managed_body(original, report, "# Current report\n", state, "https://example.test/run")

    assert updated.startswith("Human preface\n")
    assert updated.endswith("\nHuman footer")
    assert "# Current report" in updated
    assert audit.parse_state(updated) == state


def test_state_and_rendering_characterization() -> None:
    report = report_with_changes()
    applied = audit.make_applied(report, "# Current report\n", audit.canonical_items(report))
    state = audit.make_state(audit.campaign_id(spec_lock()), 0, applied)
    body = audit.managed_body("", report, "# Current report\n", state, "")

    current_report = report_with_changes(live_checksum="newer")
    current_report["changes"]["changed"] = []
    current = audit.make_applied(current_report, "# Report\n", audit.canonical_items(current_report))
    value = audit.batch_id(state["campaign"], 1, applied["semantic_digest"], current["semantic_digest"])
    comment = audit.transition_comment(current_report, applied, current, state["campaign"], 1, value, "")

    assert hashlib.sha256(audit.state_marker(state).encode()).hexdigest() == (
        "33fc5cbda9fd109c9c8330b0b15afe26fc7a08348d4e83d997bfba0d8a85a1dd"
    )
    assert hashlib.sha256(body.encode()).hexdigest() == (
        "d8af32ac867ed1aec7a6351f27dd455687009a7437a4c57207bfa41fb44a6a31"
    )
    assert hashlib.sha256(comment.encode()).hexdigest() == (
        "f12f2982578d2dc338c31cc68ed3043bcf545e71a6eb0d4287c3d41ca35deb9f"
    )


def test_transition_comment_lists_all_net_changes() -> None:
    previous_report = report_with_changes()
    current_report = report_with_changes(live_checksum="newer")
    current_report["changes"]["changed"] = []
    previous = audit.make_applied(previous_report, "# Report\n", audit.canonical_items(previous_report))
    current = audit.make_applied(current_report, "# Report\n", audit.canonical_items(current_report))
    campaign = audit.campaign_id(spec_lock())
    value = audit.batch_id(campaign, 1, previous["semantic_digest"], current["semantic_digest"])

    comment = audit.transition_comment(current_report, previous, current, campaign, 1, value, "")

    assert "- New: 0" in comment
    assert "- Updated: 1" in comment
    assert "- Resolved: 1" in comment
    assert "`fls_added`" in comment
    assert "`fls_changed`" in comment
    marker = audit.parse_batch_marker(comment)
    assert marker is not None and marker["batch_id"] == value
    assert marker["previous_semantic_digest"] == previous["semantic_digest"]


def test_compact_comment_fails_before_silent_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    report = report_with_changes()
    previous = audit.make_applied(report, "# Report\n", {})
    current = audit.make_applied(report, "# Report\n", audit.canonical_items(report))
    campaign = audit.campaign_id(spec_lock())
    value = audit.batch_id(campaign, 1, previous["semantic_digest"], current["semantic_digest"])
    monkeypatch.setattr(audit, "MAX_COMMENT_BODY_BYTES", 20)

    with pytest.raises(audit.AuditIssueError, match="Compact transition comment"):
        audit.transition_comment(report, previous, current, campaign, 1, value, "")


def test_state_requires_consistent_schema_and_managed_region() -> None:
    report = report_with_changes()
    applied = audit.make_applied(report, "# Report\n", audit.canonical_items(report))
    state = audit.make_state(audit.campaign_id(spec_lock()), 0, applied)

    damaged = copy.deepcopy(state)
    damaged["sequence"] = True
    with pytest.raises(audit.AuditIssueError, match="invalid campaign or sequence"):
        audit.validate_state(damaged)

    damaged = copy.deepcopy(state)
    damaged["applied"]["items"] = {}
    with pytest.raises(audit.AuditIssueError, match="does not match"):
        audit.validate_state(damaged)

    damaged = copy.deepcopy(state)
    damaged["origin_semantic_digest"] = "sha256:" + "0" * 64
    with pytest.raises(audit.AuditIssueError, match="sequence-zero state"):
        audit.validate_state(damaged)

    with pytest.raises(audit.AuditIssueError, match="outside its managed region"):
        audit.parse_state(audit.state_marker(state))

    reversed_region = f"{audit.MANAGED_END}\n{audit.state_marker(state)}\n{audit.MANAGED_START}"
    with pytest.raises(audit.AuditIssueError, match="boundaries are reversed"):
        audit.parse_state(reversed_region)


def test_comment_recovery_rejects_sequence_gap() -> None:
    report = report_with_changes()
    current_report = report_with_changes(live_checksum="new")
    previous = audit.make_applied(report, "# Report\n", audit.canonical_items(report))
    current = audit.make_applied(current_report, "# Report\n", audit.canonical_items(current_report))
    campaign = audit.campaign_id(spec_lock())
    state = audit.make_state(campaign, 0, previous)
    value = audit.batch_id(campaign, 2, previous["semantic_digest"], current["semantic_digest"])
    comment = {
        "body": audit.batch_marker(campaign, 2, value, current, previous["semantic_digest"]),
        "user": {"login": audit.ACTIONS_BOT_LOGIN, "id": audit.ACTIONS_BOT_ID, "type": "Bot"},
    }

    with pytest.raises(audit.AuditIssueError, match="sequence jumps"):
        audit.recover_from_comments(state, [comment])


def test_first_transition_is_grounded_in_campaign_origin() -> None:
    report = report_with_changes()
    current_report = report_with_changes(live_checksum="new")
    origin = audit.make_applied(report, "# Report\n", audit.canonical_items(report))
    current = audit.make_applied(current_report, "# Report\n", audit.canonical_items(current_report))
    campaign = audit.campaign_id(spec_lock())
    state = audit.make_state(campaign, 1, current, origin["semantic_digest"])
    wrong_previous = "sha256:" + "0" * 64
    value = audit.batch_id(campaign, 1, wrong_previous, current["semantic_digest"])
    comment = {
        "body": audit.batch_marker(campaign, 1, value, current, wrong_previous),
        "user": {"login": audit.ACTIONS_BOT_LOGIN, "id": audit.ACTIONS_BOT_ID, "type": "Bot"},
    }

    with pytest.raises(audit.AuditIssueError, match="invalid historical batch chain"):
        audit.verify_comment_history(state, [comment])


def test_compact_issue_body_ignores_workflow_url_for_idempotence() -> None:
    report = report_with_changes()
    report_md = f"# Report\n{'x' * audit.MAX_ISSUE_BODY_BYTES}\n"
    applied = audit.make_applied(report, report_md, audit.canonical_items(report))
    state = audit.make_state(audit.campaign_id(spec_lock()), 0, applied)

    first = audit.managed_body("", report, report_md, state, "https://example.test/runs/1")
    second = audit.managed_body("", report, report_md, state, "https://example.test/runs/2")

    assert "Complete workflow artifact:" in first
    assert audit.comparable_managed_body(first) == audit.comparable_managed_body(second)
