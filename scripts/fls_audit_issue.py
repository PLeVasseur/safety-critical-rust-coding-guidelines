import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from scripts.fls_audit_issue_lib.errors import AuditIssueError
from scripts.fls_audit_issue_lib.github import GitHubClient

DEFAULT_LABEL = "fls-audit"
DEFAULT_TITLE_PREFIX = "FLS audit:"
LEGACY_ISSUE_NUMBER = 1236
ACTIONS_BOT_LOGIN = "github-actions[bot]"
ACTIONS_BOT_ID = 41898282
MAX_ISSUE_BODY_BYTES = 60_000
MAX_COMMENT_BODY_BYTES = 60_000
CONFIRMATION_DELAYS = (0, 1, 2, 4, 8)
VERIFICATION_DELAYS = (0, 1, 2)

MANAGED_START = "<!-- fls-audit:managed:start -->"
MANAGED_END = "<!-- fls-audit:managed:end -->"
STATE_RE = re.compile(r"<!-- fls-audit:state:v1\n(?P<state>\{.*?\})\n-->", re.DOTALL)
BATCH_RE = re.compile(r"<!-- fls-audit:batch:v1\n(?P<marker>\{.*?\})\n-->", re.DOTALL)
LEGACY_BASELINE_RE = re.compile(r"^- Baseline commit: `(?P<commit>[0-9a-f]{40})`$", re.MULTILINE)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

BODY_DIGEST_FIELDS = (
    "affected_guidelines",
    "changes",
    "header_changes",
    "new_paragraph_assessments",
    "relevance",
    "section_reorders",
    "summary",
    "text",
)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AuditIssueError(f"Missing required environment variable: {name}")
    return value


def compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: object) -> str:
    return f"sha256:{hashlib.sha256(compact_json(value).encode()).hexdigest()}"


def check_size(value: str, limit: int, description: str) -> None:
    size = len(value.encode("utf-8"))
    if size > limit:
        raise AuditIssueError(f"{description} is {size} bytes; limit is {limit} bytes")


def campaign_id(spec_lock: dict[str, Any]) -> str:
    documents = spec_lock.get("documents")
    if not isinstance(documents, (dict, list)):
        raise AuditIssueError("spec.lock does not contain documents")
    return sha256_json(documents)


def paragraph_side(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"checksum": "", "section": ""}
    return {
        "checksum": str(value.get("checksum", "")),
        "section": str(value.get("section_id", "")),
    }


def canonical_items(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    changes = report.get("changes", {})
    if not isinstance(changes, dict):
        raise AuditIssueError("Audit report changes must be an object")

    for kind in ("added", "removed", "changed"):
        entries = changes.get(kind, [])
        if not isinstance(entries, list):
            raise AuditIssueError(f"Audit report changes.{kind} must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("fls_id"):
                raise AuditIssueError(f"Audit report changes.{kind} contains an invalid entry")
            fls_id = str(entry["fls_id"])
            key = f"paragraph:{fls_id}"
            if key in items:
                raise AuditIssueError(f"Audit report contains duplicate paragraph {fls_id}")
            item: dict[str, Any] = {
                "kind": kind,
                "locked": paragraph_side(entry.get("locked")),
                "live": paragraph_side(entry.get("live")),
            }
            if kind == "changed":
                item["content_changed"] = bool(entry.get("content_changed"))
                item["section_changed"] = bool(entry.get("section_changed"))
            items[key] = item

    for report_key, prefix in (("header_changes", "header"), ("section_reorders", "reorder")):
        entries = report.get(report_key, [])
        if not isinstance(entries, list):
            raise AuditIssueError(f"Audit report {report_key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise AuditIssueError(f"Audit report {report_key} contains an invalid entry")
            section_id = entry.get("section_id") or entry.get("fls_id")
            if not section_id:
                raise AuditIssueError(f"Audit report {report_key} entry has no section identity")
            key = f"{prefix}:{section_id}"
            if key in items:
                raise AuditIssueError(f"Audit report contains duplicate item {key}")
            items[key] = {"kind": prefix, "value": entry}

    return dict(sorted(items.items()))


def normalized_report_markdown(report_md: str) -> str:
    volatile_prefixes = ("- Generated: ", "- Spec lock: ")
    return "\n".join(line for line in report_md.rstrip().splitlines() if not line.startswith(volatile_prefixes))


def report_body_digest(report: dict[str, Any], report_md: str) -> str:
    return sha256_json(
        {
            "fields": {key: report.get(key) for key in BODY_DIGEST_FIELDS},
            "markdown": normalized_report_markdown(report_md),
        }
    )


def report_commit(report: dict[str, Any], key: str) -> str:
    metadata = report.get("metadata", {})
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get(key)
    return str(value) if value else ""


def make_applied(report: dict[str, Any], report_md: str, items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "semantic_digest": sha256_json(items),
        "body_digest": report_body_digest(report, report_md),
        "current_commit": report_commit(report, "current_commit"),
        "items": items,
    }


def validate_applied(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"semantic_digest", "body_digest", "current_commit", "items"}:
        raise AuditIssueError("FLS audit applied state has an invalid schema")
    items = value.get("items")
    if not isinstance(items, dict) or not all(isinstance(key, str) and isinstance(item, dict) for key, item in items.items()):
        raise AuditIssueError("FLS audit applied state has no item map")
    if not all(isinstance(value.get(key), str) and DIGEST_RE.fullmatch(value[key]) for key in ("semantic_digest", "body_digest")):
        raise AuditIssueError("FLS audit applied state has invalid digests")
    if value["semantic_digest"] != sha256_json(items):
        raise AuditIssueError("FLS audit applied state digest does not match its item map")
    commit = value.get("current_commit")
    if not isinstance(commit, str) or (commit and not COMMIT_RE.fullmatch(commit)):
        raise AuditIssueError("FLS audit applied state has an invalid current commit")
    return value


def make_state(
    campaign: str,
    sequence: int,
    applied: dict[str, Any],
    origin_semantic_digest: str | None = None,
) -> dict[str, Any]:
    applied = validate_applied(applied)
    origin = origin_semantic_digest or applied["semantic_digest"]
    return {
        "version": 1,
        "campaign": campaign,
        "sequence": sequence,
        "origin_semantic_digest": origin,
        "applied": applied,
    }


def validate_state(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "campaign", "sequence", "origin_semantic_digest", "applied"}
        or value.get("version") != 1
    ):
        raise AuditIssueError("Unsupported or malformed FLS audit issue state")
    sequence = value.get("sequence")
    if not isinstance(value.get("campaign"), str) or not DIGEST_RE.fullmatch(value["campaign"]):
        raise AuditIssueError("FLS audit issue state has invalid campaign or sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise AuditIssueError("FLS audit issue state has invalid campaign or sequence")
    origin = value.get("origin_semantic_digest")
    if not isinstance(origin, str) or not DIGEST_RE.fullmatch(origin):
        raise AuditIssueError("FLS audit issue state has an invalid origin semantic digest")
    applied = validate_applied(value.get("applied"))
    if sequence == 0 and origin != applied["semantic_digest"]:
        raise AuditIssueError("FLS audit issue origin does not match its sequence-zero state")
    return value


def managed_span(body: str) -> tuple[int, int] | None:
    start_count = body.count(MANAGED_START)
    end_count = body.count(MANAGED_END)
    if start_count == 0 and end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise AuditIssueError("FLS audit issue managed region is missing or ambiguous")
    start = body.index(MANAGED_START)
    end = body.index(MANAGED_END)
    if end < start:
        raise AuditIssueError("FLS audit issue managed region boundaries are reversed")
    return start, end + len(MANAGED_END)


def parse_state(body: str) -> dict[str, Any] | None:
    check_size(body, MAX_ISSUE_BODY_BYTES, "Existing issue body")
    matches = list(STATE_RE.finditer(body))
    if not matches:
        if "fls-audit:state:" in body:
            raise AuditIssueError("FLS audit issue contains an unsupported state marker")
        if managed_span(body) is not None:
            raise AuditIssueError("FLS audit issue managed region has no state marker")
        return None
    if len(matches) != 1:
        raise AuditIssueError("FLS audit issue contains multiple state markers")
    span = managed_span(body)
    if span is None or not (span[0] < matches[0].start() and matches[0].end() < span[1]):
        raise AuditIssueError("FLS audit issue state marker is outside its managed region")
    try:
        return validate_state(json.loads(matches[0].group("state")))
    except json.JSONDecodeError as error:
        raise AuditIssueError("FLS audit issue state is not valid JSON") from error


def state_marker(state: dict[str, Any]) -> str:
    return f"<!-- fls-audit:state:v1\n{compact_json(state)}\n-->"


def replace_managed(body: str, managed: str) -> str:
    span = managed_span(body)
    if span is None:
        return f"{body.rstrip()}\n\n{managed}" if body else managed
    return f"{body[: span[0]]}{managed}{body[span[1] :]}"


def comparable_managed_body(body: str) -> str:
    span = managed_span(body)
    if span is None:
        raise AuditIssueError("FLS audit issue has no managed region")
    managed = STATE_RE.sub("<!-- fls-audit:state -->", body[span[0] : span[1]])
    volatile_prefixes = (
        "- Generated at: ",
        "- Workflow run: ",
        "- Generated: ",
        "- Spec lock: ",
        "Complete workflow artifact: ",
    )
    return "\n".join(line for line in managed.splitlines() if not line.startswith(volatile_prefixes))


def run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return ""


def build_instructions(report: dict[str, Any], workflow_url: str) -> str:
    lines = [
        "## What to do",
        "- Review the current cumulative report below.",
        "- If no guideline updates are required, comment `@guidelines-bot /accept-no-fls-changes` (triage+ only).",
        "- If guideline updates are required, open a synchronization PR and include `Closes #<this issue>`.",
        "- See `docs/fls-audit.md` for the audit workflow.",
        "",
        "## Current audit",
    ]
    generated_at = report.get("metadata", {}).get("generated_at")
    if generated_at:
        lines.append(f"- Generated at: `{generated_at}`")
    if workflow_url:
        lines.append(f"- Workflow run: {workflow_url}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def item_summary(key: str, item: dict[str, Any]) -> str:
    kind = str(item.get("kind", "changed"))
    if not key.startswith("paragraph:"):
        return f"`{key}`: {kind}"
    fls_id = key.removeprefix("paragraph:")
    locked = item.get("locked", {})
    live = item.get("live", {})
    locked_section = str(locked.get("section", "")) if isinstance(locked, dict) else ""
    live_section = str(live.get("section", "")) if isinstance(live, dict) else ""
    sections = f"; section `{locked_section or '-'} -> {live_section or '-'}`" if locked_section or live_section else ""
    return f"`{fls_id}`: {kind}{sections}"


def compact_report(report: dict[str, Any], items: dict[str, dict[str, Any]], workflow_url: str) -> str:
    lines = [
        "# FLS Spec Lock Audit Report",
        "",
        "The complete report exceeded the issue body budget. Every active drift item is listed below.",
        "",
        "## Active drift",
    ]
    lines.extend(f"- {item_summary(key, item)}" for key, item in items.items())
    if not items:
        lines.append("- None")
    affected = report.get("affected_guidelines", {})
    lines.extend(["", "## Affected guidelines"])
    if isinstance(affected, dict) and affected:
        for guideline_id, value in sorted(affected.items()):
            title = value.get("title", "Untitled") if isinstance(value, dict) else "Untitled"
            lines.append(f"- `{guideline_id}`: {title}")
    else:
        lines.append("- None")
    if workflow_url:
        lines.extend(["", f"Complete workflow artifact: {workflow_url}"])
    return "\n".join(lines)


def managed_body(
    existing_body: str,
    report: dict[str, Any],
    report_md: str,
    state: dict[str, Any],
    workflow_url: str,
) -> str:
    instructions = build_instructions(report, workflow_url)
    managed = f"{MANAGED_START}\n{instructions}{report_md.rstrip()}\n\n{state_marker(state)}\n{MANAGED_END}"
    candidate = replace_managed(existing_body, managed)
    if len(candidate.encode("utf-8")) <= MAX_ISSUE_BODY_BYTES:
        return candidate
    compact = compact_report(report, state["applied"]["items"], workflow_url)
    managed = f"{MANAGED_START}\n{instructions}{compact}\n\n{state_marker(state)}\n{MANAGED_END}"
    candidate = replace_managed(existing_body, managed)
    check_size(candidate, MAX_ISSUE_BODY_BYTES, "Compact issue body")
    return candidate


def diff_items(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    new = sorted(current.keys() - previous.keys())
    updated = sorted(key for key in current.keys() & previous.keys() if current[key] != previous[key])
    resolved = sorted(previous.keys() - current.keys())
    return new, updated, resolved


def affected_guidelines(report: dict[str, Any]) -> list[str]:
    affected = report.get("affected_guidelines", {})
    if not isinstance(affected, dict) or not affected:
        return ["- None"]
    return [
        f"- `{guideline_id}`: {value.get('title', 'Untitled') if isinstance(value, dict) else 'Untitled'}"
        for guideline_id, value in sorted(affected.items())
    ]


def text_diffs(report: dict[str, Any], fls_ids: set[str]) -> list[str]:
    text = report.get("text", {})
    entries = text.get("content_diffs", []) if isinstance(text, dict) else []
    lines: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or str(entry.get("fls_id")) not in fls_ids:
            continue
        diff = entry.get("diff", [])
        if not isinstance(diff, list) or not diff:
            continue
        lines.extend(
            [
                "",
                f"<details><summary>{entry['fls_id']} current lock-to-live text</summary>",
                "",
                "```diff",
                *(str(line) for line in diff),
                "```",
                "",
                "</details>",
            ]
        )
    return lines


def batch_id(campaign: str, sequence: int, previous_digest: str, target_digest: str) -> str:
    return sha256_json(
        {"campaign": campaign, "sequence": sequence, "previous": previous_digest, "target": target_digest}
    )


def batch_marker(
    campaign: str,
    sequence: int,
    value: str,
    applied: dict[str, Any] | None = None,
    previous_semantic_digest: str | None = None,
) -> str:
    marker: dict[str, Any] = {"batch_id": value, "campaign": campaign, "sequence": sequence}
    if applied is not None:
        if previous_semantic_digest is None:
            raise AuditIssueError("FLS audit transition marker has no previous semantic digest")
        marker["previous_semantic_digest"] = previous_semantic_digest
        marker["applied"] = applied
    return f"<!-- fls-audit:batch:v1\n{compact_json(marker)}\n-->"


def parse_batch_marker(body: str) -> dict[str, Any] | None:
    check_size(body, MAX_COMMENT_BODY_BYTES, "Existing audit comment")
    matches = list(BATCH_RE.finditer(body))
    if not matches:
        if "fls-audit:batch:" in body:
            raise AuditIssueError("FLS audit comment contains an unsupported batch marker")
        return None
    if len(matches) != 1:
        raise AuditIssueError("FLS audit comment contains multiple batch markers")
    try:
        marker = json.loads(matches[0].group("marker"))
    except json.JSONDecodeError as error:
        raise AuditIssueError("FLS audit batch marker is not valid JSON") from error
    base = {"batch_id", "campaign", "sequence"}
    if not isinstance(marker, dict) or not base <= set(marker):
        raise AuditIssueError("FLS audit batch marker has an invalid schema")
    expected = base | ({"previous_semantic_digest", "applied"} if "applied" in marker else set())
    if set(marker) != expected:
        raise AuditIssueError("FLS audit batch marker has an invalid schema")
    sequence = marker.get("sequence")
    if not isinstance(marker.get("batch_id"), str) or not DIGEST_RE.fullmatch(marker["batch_id"]):
        raise AuditIssueError("FLS audit batch marker has an invalid batch ID")
    if not isinstance(marker.get("campaign"), str) or not marker["campaign"]:
        raise AuditIssueError("FLS audit batch marker has an invalid campaign")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise AuditIssueError("FLS audit batch marker has an invalid sequence")
    if "applied" in marker:
        previous = marker.get("previous_semantic_digest")
        if not isinstance(previous, str) or not DIGEST_RE.fullmatch(previous):
            raise AuditIssueError("FLS audit batch marker has an invalid previous semantic digest")
        validate_applied(marker["applied"])
    return marker


def transition_comment(
    report: dict[str, Any],
    previous: dict[str, Any],
    current: dict[str, Any],
    campaign: str,
    sequence: int,
    value: str,
    workflow_url: str,
    *,
    include_diffs: bool = True,
) -> str:
    previous_items = previous["items"]
    current_items = current["items"]
    new, updated, resolved = diff_items(previous_items, current_items)
    lines = [
        "## FLS drift update",
        "",
        "Net changes since the previous successfully applied bot state:",
        "",
        f"- New: {len(new)}",
        f"- Updated: {len(updated)}",
        f"- Resolved: {len(resolved)}",
    ]
    if previous.get("current_commit"):
        lines.append(f"- Previous FLS commit: `{previous['current_commit']}`")
    if current.get("current_commit"):
        lines.append(f"- Current FLS commit: `{current['current_commit']}`")
    if workflow_url:
        lines.append(f"- Workflow run: {workflow_url}")
    for heading, keys, source in (
        ("New", new, current_items),
        ("Updated", updated, current_items),
        ("Resolved", resolved, previous_items),
    ):
        lines.extend(["", f"### {heading}"])
        lines.extend(f"- {item_summary(key, source[key])}" for key in keys)
        if not keys:
            lines.append("- None")
    if include_diffs:
        ids = {key.removeprefix("paragraph:") for key in new + updated if key.startswith("paragraph:")}
        lines.extend(text_diffs(report, ids))
    lines.extend(["", "### Currently affected guidelines", *affected_guidelines(report), ""])
    lines.append(batch_marker(campaign, sequence, value, current, previous["semantic_digest"]))
    body = "\n".join(lines)
    if len(body.encode("utf-8")) <= MAX_COMMENT_BODY_BYTES:
        return body
    if include_diffs:
        return transition_comment(
            report,
            previous,
            current,
            campaign,
            sequence,
            value,
            workflow_url,
            include_diffs=False,
        )
    check_size(body, MAX_COMMENT_BODY_BYTES, "Compact transition comment")
    return body


def issue_labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels", [])
    if not isinstance(labels, list):
        return []
    return sorted(str(label["name"]) for label in labels if isinstance(label, dict) and label.get("name"))


def is_actions_bot_record(value: dict[str, Any]) -> bool:
    user = value.get("user")
    return (
        isinstance(user, dict)
        and user.get("login") == ACTIONS_BOT_LOGIN
        and user.get("id") == ACTIONS_BOT_ID
        and user.get("type") == "Bot"
    )


def audit_issues(issues: list[dict[str, Any]], title_prefix: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    result = []
    for issue in issues:
        if "pull_request" in issue or not is_actions_bot_record(issue):
            continue
        body = str(issue.get("body") or "")
        state = parse_state(body)
        if state is not None or str(issue.get("title") or "").startswith(title_prefix):
            result.append((issue, state))
    return result


def find_campaign(
    issues: list[tuple[dict[str, Any], dict[str, Any] | None]],
    campaign: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [(issue, state) for issue, state in issues if state and state.get("campaign") == campaign]
    if len(matches) > 1:
        numbers = ", ".join(f"#{issue.get('number')}" for issue, _ in matches)
        raise AuditIssueError(f"Multiple FLS audit issues claim campaign {campaign}: {numbers}")
    return matches[0] if matches else None


def expected_title(title_prefix: str, campaign: str) -> str:
    return f"{title_prefix} spec.lock drift ({campaign.removeprefix('sha256:')[:12]})"


def refresh_issue_identity(client: GitHubClient, issue: dict[str, Any], title: str, label: str) -> tuple[dict[str, Any], bool]:
    patch: dict[str, Any] = {}
    if issue.get("title") != title:
        patch["title"] = title
    labels = issue_labels(issue)
    if label not in labels:
        patch["labels"] = sorted([*labels, label])
    return (client.patch_issue(int(issue["number"]), patch), True) if patch else (issue, False)


def comment_markers(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers = [
        marker
        for comment in comments
        if is_actions_bot_record(comment) and (marker := parse_batch_marker(str(comment.get("body") or ""))) is not None
    ]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for marker in markers:
        value = marker["batch_id"]
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        values = ", ".join(sorted(duplicates))
        raise AuditIssueError(f"FLS audit issue contains duplicate bot batch markers: {values}")
    return markers


def verify_comment_history(state: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    transitions = [
        marker
        for marker in comment_markers(comments)
        if marker.get("campaign") == state["campaign"] and marker.get("applied") is not None
    ]
    by_sequence: dict[int, dict[str, Any]] = {}
    for marker in transitions:
        sequence = int(marker["sequence"])
        if sequence in by_sequence:
            raise AuditIssueError(f"FLS audit issue contains multiple bot transitions at sequence {sequence}")
        by_sequence[sequence] = marker

    expected_sequences = set(range(1, int(state["sequence"]) + 1))
    actual_sequences = set(by_sequence)
    if actual_sequences != expected_sequences:
        raise AuditIssueError(
            "FLS audit comment history does not match issue sequence "
            f"{state['sequence']}: found {sorted(actual_sequences)}"
        )
    if not by_sequence:
        return

    latest = by_sequence[int(state["sequence"])]["applied"]
    if latest["semantic_digest"] != state["applied"]["semantic_digest"]:
        raise AuditIssueError("Latest FLS audit comment does not match the issue semantic state")
    for sequence in range(1, int(state["sequence"]) + 1):
        previous_digest = (
            state["origin_semantic_digest"]
            if sequence == 1
            else by_sequence[sequence - 1]["applied"]["semantic_digest"]
        )
        current = by_sequence[sequence]["applied"]
        expected = batch_id(state["campaign"], sequence, previous_digest, current["semantic_digest"])
        if by_sequence[sequence]["previous_semantic_digest"] != previous_digest or by_sequence[sequence]["batch_id"] != expected:
            raise AuditIssueError(f"FLS audit comment at sequence {sequence} has an invalid historical batch chain")


def recover_from_comments(state: dict[str, Any], comments: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    candidates = [
        marker
        for marker in comment_markers(comments)
        if marker.get("campaign") == state["campaign"] and isinstance(marker.get("sequence"), int) and marker.get("applied") is not None
    ]
    if not candidates:
        return state, False
    by_sequence: dict[int, dict[str, Any]] = {}
    for marker in candidates:
        sequence = int(marker["sequence"])
        existing = by_sequence.get(sequence)
        if existing is not None and compact_json(existing) != compact_json(marker):
            raise AuditIssueError(f"Audit comments conflict at sequence {sequence}")
        by_sequence[sequence] = marker

    same = by_sequence.get(int(state["sequence"]))
    if same and same["applied"]["semantic_digest"] != state["applied"]["semantic_digest"]:
        raise AuditIssueError("Issue state conflicts with an audit comment at the same sequence")

    previous = state["applied"]
    expected_sequence = int(state["sequence"]) + 1
    recovered = False
    for sequence, marker in sorted(by_sequence.items()):
        if sequence < expected_sequence:
            continue
        if sequence != expected_sequence:
            raise AuditIssueError(f"Audit comment sequence jumps from {expected_sequence - 1} to {sequence}")
        applied = marker["applied"]
        expected_batch = batch_id(
            state["campaign"], sequence, previous["semantic_digest"], applied["semantic_digest"]
        )
        if marker["previous_semantic_digest"] != previous["semantic_digest"] or marker["batch_id"] != expected_batch:
            raise AuditIssueError(f"Audit comment at sequence {sequence} has an invalid batch chain")
        previous = applied
        expected_sequence += 1
        recovered = True
    return (
        make_state(state["campaign"], expected_sequence - 1, previous, state["origin_semantic_digest"])
        if recovered
        else state,
        recovered,
    )


def post_comment_once(
    client: GitHubClient,
    issue_number: int,
    body: str,
    value: str,
    comments: list[dict[str, Any]],
) -> None:
    if any(marker.get("batch_id") == value for marker in comment_markers(comments)):
        return
    try:
        client.post_comment(issue_number, body)
    except (AuditIssueError, requests.RequestException) as write_error:
        for delay in CONFIRMATION_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                if any(
                    marker.get("batch_id") == value for marker in comment_markers(client.comments(issue_number))
                ):
                    return
            except (AuditIssueError, requests.RequestException):
                continue
        raise write_error


def reconcile_campaign(
    client: GitHubClient,
    issue: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
    report_md: str,
    current: dict[str, Any],
    workflow_url: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    number = int(issue["number"])
    starting_state = state
    comments = client.comments(number)
    state, recovered = recover_from_comments(state, comments)
    previous = state["applied"]
    transitioned = previous["semantic_digest"] != current["semantic_digest"]
    if previous["semantic_digest"] == current["semantic_digest"]:
        if recovered or previous["body_digest"] != current["body_digest"]:
            state = make_state(state["campaign"], state["sequence"], current, state["origin_semantic_digest"])
    else:
        sequence = int(state["sequence"]) + 1
        value = batch_id(state["campaign"], sequence, previous["semantic_digest"], current["semantic_digest"])
        comment = transition_comment(report, previous, current, state["campaign"], sequence, value, workflow_url)
        next_state = make_state(state["campaign"], sequence, current, state["origin_semantic_digest"])
        managed_body(str(issue.get("body") or ""), report, report_md, next_state, workflow_url)
        post_comment_once(client, number, comment, value, comments)
        state = next_state
    latest = client.issue(number)
    latest_body = str(latest.get("body") or "")
    latest_state = parse_state(latest_body)
    if latest_state is None or latest_state["campaign"] != state["campaign"]:
        raise AuditIssueError(f"Audit issue #{number} lost its current campaign state before update")
    if compact_json(latest_state) != compact_json(starting_state):
        if compact_json(latest_state) != compact_json(state):
            raise AuditIssueError(f"Audit issue #{number} changed concurrently before update")
        starting_state = latest_state
    body = managed_body(latest_body, report, report_md, state, workflow_url)
    state_changed = compact_json(starting_state) != compact_json(state)
    if not transitioned and not recovered and not state_changed:
        if comparable_managed_body(latest_body) == comparable_managed_body(body):
            return latest, state, False
    return client.patch_issue(number, {"body": body}), state, True


def legacy_baseline(issue: dict[str, Any], label: str, title_prefix: str) -> str | None:
    if (
        int(issue.get("number", 0)) != LEGACY_ISSUE_NUMBER
        or "pull_request" in issue
        or not is_actions_bot_record(issue)
    ):
        return None
    if label not in issue_labels(issue) or not str(issue.get("title") or "").startswith(f"{title_prefix} changes detected ("):
        return None
    body = str(issue.get("body") or "")
    if MANAGED_START in body or "fls-audit:state:" in body:
        return None
    if "## What to do" not in body or "# FLS Spec Lock Audit Report" not in body:
        raise AuditIssueError(f"Legacy audit issue #{LEGACY_ISSUE_NUMBER} has an unrecognized body")
    matches = list(LEGACY_BASELINE_RE.finditer(body))
    if len(matches) != 1:
        raise AuditIssueError(
            f"Pre-campaign audit issue #{LEGACY_ISSUE_NUMBER} must contain exactly one baseline commit"
        )
    return matches[0].group("commit")


def archived_legacy_body(body: str) -> str:
    return "\n".join(
        [
            "<details>",
            "<summary>Pre-campaign audit body</summary>",
            "",
            body.rstrip(),
            "",
            "</details>",
        ]
    )


def create_issue_safely(
    client: GitHubClient,
    title: str,
    body: str,
    label: str,
    campaign: str,
    title_prefix: str,
) -> dict[str, Any]:
    try:
        return client.create_issue(title, body, label)
    except (AuditIssueError, requests.RequestException) as write_error:
        for delay in CONFIRMATION_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                match = find_campaign(audit_issues(client.issues(), title_prefix), campaign)
                if match:
                    return match[0]
            except (AuditIssueError, requests.RequestException):
                continue
        raise write_error


def event_comment(campaign: str, value: str, message: str, workflow_url: str) -> str:
    lines = [message]
    if workflow_url:
        lines.extend(["", f"Workflow run: {workflow_url}"])
    lines.extend(["", batch_marker(campaign, 0, value)])
    return "\n".join(lines)


def close_old_campaigns(
    client: GitHubClient,
    issues: list[tuple[dict[str, Any], dict[str, Any] | None]],
    current_campaign: str,
    current_issue_number: int | None,
    workflow_url: str,
    has_drift: bool,
) -> bool:
    changed = False
    for issue, state in sorted(issues, key=lambda value: int(value[0].get("number", 0))):
        if not state or state["campaign"] == current_campaign or issue.get("state") != "open" or issue.get("number") == current_issue_number:
            continue
        number = int(issue["number"])
        value = sha256_json({"old": state["campaign"], "new": current_campaign, "type": "superseded"})
        suffix = "A new campaign tracks the remaining drift." if has_drift else "The new baseline is currently clean."
        message = f"The committed `spec.lock` baseline changed, so this audit campaign is superseded. {suffix}"
        post_comment_once(client, number, event_comment(state["campaign"], value, message, workflow_url), value, client.comments(number))
        client.patch_issue(number, {"state": "closed", "state_reason": "completed"})
        changed = True
    return changed


def close_legacy_issue(
    client: GitHubClient,
    issue: dict[str, Any],
    campaign: str,
    workflow_url: str,
    *,
    baseline_changed: bool,
) -> bool:
    if issue.get("state") != "open":
        return False
    kind = "legacy-baseline-changed" if baseline_changed else "legacy-clean"
    value = sha256_json({"issue": LEGACY_ISSUE_NUMBER, "campaign": campaign, "type": kind})
    message = (
        "The committed `spec.lock` baseline changed, so this legacy audit issue is superseded."
        if baseline_changed
        else "The current audit is clean, so this legacy audit issue is complete."
    )
    post_comment_once(client, LEGACY_ISSUE_NUMBER, event_comment("legacy", value, message, workflow_url), value, client.comments(LEGACY_ISSUE_NUMBER))
    client.patch_issue(LEGACY_ISSUE_NUMBER, {"state": "closed", "state_reason": "completed"})
    return True


def verify_reconciliation_once(
    client: GitHubClient,
    campaign: str,
    issue_number: int | None,
    expected_state: dict[str, Any] | None,
    title: str,
    title_prefix: str,
    label: str,
    has_drift: bool,
) -> None:
    issue_values = audit_issues(client.issues(), title_prefix)
    find_campaign(issue_values, campaign)
    obsolete = [
        issue
        for issue, state in issue_values
        if issue.get("state") == "open" and (state is None or state["campaign"] != campaign)
    ]
    if obsolete:
        numbers = ", ".join(f"#{issue.get('number')}" for issue in obsolete)
        raise AuditIssueError(f"Obsolete FLS audit issues remain open after reconciliation: {numbers}")

    if issue_number is None:
        if any(state and state["campaign"] == campaign for _, state in issue_values):
            raise AuditIssueError("Current FLS audit campaign exists unexpectedly after reconciliation")
        return
    if expected_state is None:
        raise AuditIssueError("Current FLS audit issue has no expected state")

    records = [issue for issue, _ in issue_values if int(issue.get("number", 0)) == issue_number]
    if len(records) != 1:
        raise AuditIssueError(f"Expected one current FLS audit issue #{issue_number}; found {len(records)}")
    issue = records[0]
    state = parse_state(str(issue.get("body") or ""))
    if state is None or compact_json(state) != compact_json(expected_state):
        raise AuditIssueError(f"FLS audit issue #{issue_number} state does not match the reconciled report")
    if issue.get("title") != title or label not in issue_labels(issue):
        raise AuditIssueError(f"FLS audit issue #{issue_number} identity does not match the current campaign")
    expected_status = "open" if has_drift else "closed"
    if issue.get("state") != expected_status:
        raise AuditIssueError(
            f"FLS audit issue #{issue_number} is {issue.get('state')}; expected {expected_status}"
        )


def verify_reconciliation(
    client: GitHubClient,
    campaign: str,
    issue_number: int | None,
    expected_state: dict[str, Any] | None,
    title: str,
    title_prefix: str,
    label: str,
    has_drift: bool,
) -> None:
    issue_error: Exception | None = None
    for delay in VERIFICATION_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            verify_reconciliation_once(
                client, campaign, issue_number, expected_state, title, title_prefix, label, has_drift
            )
        except (AuditIssueError, requests.RequestException) as current_error:
            issue_error = current_error
            continue
        break
    else:
        assert issue_error is not None
        raise issue_error

    if issue_number is None:
        return
    assert expected_state is not None
    comment_error: Exception | None = None
    for delay in VERIFICATION_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            verify_comment_history(expected_state, client.comments(issue_number))
        except (AuditIssueError, requests.RequestException) as current_error:
            comment_error = current_error
            continue
        return
    assert comment_error is not None
    raise comment_error


def reconcile(
    client: GitHubClient,
    report: dict[str, Any],
    report_md: str,
    spec_lock: dict[str, Any],
    label: str,
    title_prefix: str,
) -> str:
    client.ensure_label(label)
    campaign = campaign_id(spec_lock)
    items = canonical_items(report)
    current = make_applied(report, report_md, items)
    workflow_url = run_url()
    title = expected_title(title_prefix, campaign)
    all_issues = client.issues()
    audit_issue_values = audit_issues(all_issues, title_prefix)
    match = find_campaign(audit_issue_values, campaign)
    legacy = next((value for value in all_issues if int(value.get("number", 0)) == LEGACY_ISSUE_NUMBER), None)
    legacy_recorded = legacy_baseline(legacy, label, title_prefix) if legacy else None
    damaged = [
        issue
        for issue, state in audit_issue_values
        if state is None and (int(issue.get("number", 0)) != LEGACY_ISSUE_NUMBER or legacy_recorded is None)
    ]
    if damaged:
        numbers = ", ".join(f"#{issue.get('number')}" for issue in damaged)
        raise AuditIssueError(f"Bot-owned FLS audit issues have no valid campaign state: {numbers}")
    current_baseline = report_commit(report, "baseline_commit")
    issue: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    changed = False

    if match:
        issue, state = match
        issue, identity_changed = refresh_issue_identity(client, issue, title, label)
        changed |= identity_changed
        if issue.get("state") == "closed" and items:
            issue = client.patch_issue(int(issue["number"]), {"state": "open"})
            changed = True
    elif items:
        state = make_state(campaign, 0, current)
        body = managed_body("", report, report_md, state, workflow_url)
        if legacy and legacy_recorded == current_baseline:
            body = managed_body(
                archived_legacy_body(str(legacy.get("body") or "")), report, report_md, state, workflow_url
            )
            patch: dict[str, Any] = {"title": title, "body": body}
            if legacy.get("state") == "closed":
                patch["state"] = "open"
            issue = client.patch_issue(LEGACY_ISSUE_NUMBER, patch)
        else:
            issue = create_issue_safely(client, title, body, label, campaign, title_prefix)
            parsed = parse_state(str(issue.get("body") or ""))
            state = parsed or state
        changed = True

    if issue is not None and state is not None:
        issue, state, reconciled = reconcile_campaign(client, issue, state, report, report_md, current, workflow_url)
        changed |= reconciled
        if not items and issue.get("state") == "open":
            issue = client.patch_issue(int(issue["number"]), {"state": "closed", "state_reason": "completed"})
            changed = True

    if legacy and legacy_recorded and (issue is None or int(issue["number"]) != LEGACY_ISSUE_NUMBER):
        changed |= close_legacy_issue(
            client,
            legacy,
            campaign,
            workflow_url,
            baseline_changed=legacy_recorded != current_baseline,
        )

    current_number = int(issue["number"]) if issue else None
    changed |= close_old_campaigns(client, audit_issue_values, campaign, current_number, workflow_url, bool(items))
    verify_reconciliation(client, campaign, current_number, state, title, title_prefix, label, bool(items))

    if issue is None:
        return "Reconciled old audit campaigns." if changed else "No changes found and no current campaign issue exists."
    return f"Reconciled audit issue #{issue['number']}." if changed else f"Audit issue #{issue['number']} is already current."


def load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditIssueError(f"Unable to load {description} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise AuditIssueError(f"{description} at {path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update FLS audit issues.")
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--report-md", required=True)
    parser.add_argument("--spec-lock", required=True)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--title-prefix", default=DEFAULT_TITLE_PREFIX)
    args = parser.parse_args()
    try:
        report = load_json(Path(args.report_json), "audit report")
        spec_lock = load_json(Path(args.spec_lock), "spec lock")
        report_md = Path(args.report_md).read_text(encoding="utf-8")
        client = GitHubClient(require_env("GITHUB_TOKEN"), require_env("REPO_OWNER"), require_env("REPO_NAME"))
        print(reconcile(client, report, report_md, spec_lock, args.label, args.title_prefix))
    except (AuditIssueError, OSError, requests.RequestException) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
