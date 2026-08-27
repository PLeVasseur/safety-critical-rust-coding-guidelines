# FLS Audit Guide

This guide explains how to audit differences between `src/spec.lock` and the
current Ferrocene Language Specification (FLS).

## Quick start

```shell
uv run python scripts/fls_audit.py --summary-only
uv run python scripts/fls_audit.py
```

## What the audit does

- Compares `src/spec.lock` against the live FLS paragraph IDs.
- Groups changes into added/removed/modified/renumbered-only/header changes.
- Highlights potential guideline impact and structural reordering.

## CI enforcement policy

Normal pull request, merge queue, and push-to-`main` builds run all FLS
reference and coverage validation, but do not fail solely because the live FLS
has moved beyond `src/spec.lock`. The scheduled Nightly workflow and manually
dispatched Release Preflight enforce lock freshness and fail while it is stale.
Tagged Deploy builds require exact-commit preflight evidence and build offline
from the committed lock instead of consulting the live FLS.

Local builds enforce freshness by default. Use `--ignore-spec-lock-diff` only
when intentionally reproducing the nonblocking CI policy:

```shell
uv run --frozen make.py --ignore-spec-lock-diff
```

## Release preflight and deploy

Run the `Release Preflight` workflow against the default-branch ref containing
the commit to be released, and enter that commit's full 40-character SHA in the
required `release_sha` input. The workflow fails unless the selected ref resolves
to that exact SHA and the commit is reachable from the default branch. It then
runs the complete reusable build with live FLS freshness enforcement and records a
`release-preflight` commit status. It records `success` only when commit
validation and the complete build pass.

For a tag's first publication, Deploy requires the latest
`release-preflight` status on the tagged commit to be successful and no more
than 24 hours old. Deploy then builds with `--offline`, so the publication uses
the FLS baseline captured in that commit's `src/spec.lock`. A successful Pages
publication records a tag-specific `deploy/<tag>` status. A later deployment
of that same tag and commit may rely on the prior deployment status without
rechecking the live FLS. That authorization does not expire: it deliberately
allows the same tag and commit to be redeployed years later against their pinned
lock, even when the live FLS has moved.

Commit statuses are workflow evidence, not signed attestations. Repository
administrators and trusted workflows with status-write permission remain
inside the release trust boundary. Offline Deploy also remains dependent on
GitHub Actions, locked package availability, and GitHub Pages; it is decoupled
specifically from mutable live FLS state and is not claimed to be a
byte-for-byte reproducible build.

Release procedure:

1. Merge all intended release changes and identify the exact commit to tag.
2. Run `Release Preflight` from the ref at that commit and provide its full SHA.
3. Confirm the workflow and its `release-preflight` commit status succeeded.
4. Within 24 hours, create the version tag on that exact commit.
5. Confirm Deploy authorizes the commit, builds offline, publishes Pages, and
   records `deploy/<tag>`.

Do not move a tag to recover a failed release. If the tagged commit can still
pass preflight, run preflight for that exact commit and rerun Deploy. If it
cannot, reconcile the FLS baseline or other release failure in a new commit and
create a new version tag. A successful preflight deliberately remains valid
for first publication when the live FLS moves during the subsequent 24-hour
window; Nightly and the scheduled audit report that movement for the next
synchronization cycle. The freshness check tolerates up to five minutes of
GitHub/runner clock skew; a timestamp farther in the future fails with a
separate diagnostic.

## Rolling audit issue

The scheduled FLS Audit workflow maintains one issue for each committed
`spec.lock` baseline. The issue body contains the latest cumulative report.
When the net drift changes, the bot adds one comment listing every newly
active, updated, and resolved drift item since its last successful update.

If scheduled runs are missed, the next run posts the complete net catch-up as
one comment. Changes that appeared and were fully reverted between successful
bot observations are intentionally not reconstructed.

The workflow is also available through `workflow_dispatch`. Operational manual
runs must select the repository's default branch. Rerunning an unchanged audit
does not edit the issue or add a comment when prior writes are visible and the
stored history is valid.

Every required `build` check depends on a parallel job running the focused FLS
audit unit, integration, and workflow contract suite. After each live
reconciliation, the bot also rereads GitHub and verifies the issue identity,
open or closed status, campaign state, complete comment sequence, and unique
batch markers. A failed postcondition leaves the workflow red instead of
silently accepting incomplete or duplicate audit history.

Maintainers normally close the audit issue from the synchronization PR by
including `Closes #<issue>` in its body. If no guideline updates are required,
a maintainer with triage permission may instead comment
`@guidelines-bot /accept-no-fls-changes`; that command reruns the audit and
refuses to proceed if any guideline is affected.

## Consistency and retry limits

GitHub issue and comment POST endpoints do not support idempotency keys, so the
audit cannot guarantee exactly-once delivery. It provides best-effort uniqueness
with detectable ambiguity:

- Transition comments carry deterministic batch markers and are posted before
  the issue body advances.
- A rerun can recover a visible comment whose issue-body update failed.
- Safe reads retry transient network failures, `429`, retryable rate-limit
  `403`, and `5xx` responses for at most 15 seconds.
- `Retry-After` is honored when it fits within that budget.
- Ambiguous POST and managed-body PATCH operations are never blindly replayed.
- Duplicate, conflicting, or incomplete bot history fails closed.

The bot preflights the expected transition comment and issue body before
posting. A concurrent human edit can still make the refetched body exceed the
limit after the comment is visible; the comment marker preserves the state
needed for a later reviewed recovery.

## Trust boundary

Managed state is accepted only from REST records authored by the GitHub.com
Actions bot, `github-actions[bot]` with numeric ID `41898282`. This identity is
repository-wide: it does not identify a specific workflow or the last
maintainer who edited an issue body. Repository maintainers and other workflows
with issue-write permission remain inside the trust boundary.

The workflow concurrency group is the supported serialization mechanism.
Concurrent direct invocations of the mutating issue script are unsupported.

## Operational ownership and review

FLS reconciliation is currently a shared maintainer responsibility. A dedicated
reconciliation crew is expected in the future, but no individual owner or
response-time commitment is assigned yet. A maintainer manually starting an
audit or preparing a release is responsible for triaging the resulting failure
or recording a clear handoff.

The rolling issue is recoverable automation state and an operational review
surface, not signed safety evidence. The committed `spec.lock`, reviewed
synchronization PR, and preserved workflow reports provide the evidence used to
rationalize a baseline update. Damage to old issue state does not independently
invalidate a fresh lock or block Release Preflight; the live comparison against
the committed lock remains the release freshness criterion.

Review the reconciler and release authorization protocol after 90 days of
production use. Record the number of scheduled and no-op runs, normal
transitions, recovered partial writes,
unresolved ambiguous writes, manual repairs, operator time, actual use of
transition comments, and payload growth. Also record successful, rejected,
stale, and clock-skewed preflights; permanent redeploy authorizations; operator
confusion; and status-repair incidents. Retain either protocol only if that
evidence justifies its continuing maintenance cost.

## Troubleshooting a failed reconciliation

1. Preserve the issue body, raw comments, workflow URL, and the run's JSON,
   Markdown, and ANSI report artifacts before editing anything.
2. Read the first reconciliation or postcondition error in the workflow log.
3. If the failure is only a bounded visibility or rate-limit timeout and no
   malformed, missing, duplicate, or conflicting marker was reported, rerun the
   unchanged workflow once from the default branch.
4. If history is missing, duplicated, conflicting, or malformed, stop. Do not
   fabricate comments or manually rewrite hidden state. Use a reviewed repair
   based on the preserved evidence.
5. If a payload is oversized, use the complete workflow artifact for review and
   change the rendering/state policy in a reviewed code change; do not truncate
   the issue manually.

## One-time issue #1236 adoption

Issue `#1236` predates campaign markers. The reconciler has one narrow adoption
path for that exact issue. It requires the expected Actions bot author, title
form, `fls-audit` label, generated report headings, exactly one valid baseline
commit, and no conflicting campaign marker. The baseline must match the current
report before the issue can become the current campaign. Its complete old body
is archived outside the managed region and its comments are left untouched.

Immediately before the first production audit using this code, recheck those
assumptions. If the issue changed, stop rather than broadening the recognizer.

Remove the dedicated adoption code and tests after the first production run has
adopted or definitively closed `#1236`, the issue has managed state or is clearly
historical, and a second unchanged run performs zero issue writes. This is not a
general legacy compatibility mechanism. Remove the recognizer, pre-campaign body
archival path, legacy close path, dedicated tests, and this section in a separate
cleanup PR.

## Outputs

- `build/fls_audit/report.json`
- `build/fls_audit/report.md`
- `build/fls_audit/report.ansi.md`

## Colored diffs (delta)

The audit tool can render ANSI-colored diffs using `delta`.
When needed, it downloads a pinned delta release into `./.cache/fls-audit/tools/delta/`.
If `delta` is unavailable, the ANSI report falls back to plain unified diffs.

```shell
uv run python scripts/fls_audit.py --print-diffs
```

Overrides and opt-out:

- `--delta-path path/to/delta`
- `--no-delta`

View the ANSI report in a terminal:

```shell
less -R build/fls_audit/report.ansi.md
bat --style=plain --paging=always build/fls_audit/report.ansi.md
```

## Performance note

The audit parses only changed `.rst` files by default. If any ordering files
(`.. toctree::` or `.. appendices::`, including `:glob:` patterns) change, the
audit also parses the referenced files to keep header and reorder detection
accurate.

## Baseline and current selection

By default, the audit uses:

- Baseline: `metadata.fls_deployed_commit` from `src/spec.lock` (if present).
- Current: latest GitHub Pages deployment commit.

You can override with explicit commits:

```shell
uv run python scripts/fls_audit.py --baseline-fls-commit <sha> --current-fls-commit <sha>
```

Or use deployment offsets (relative to the latest deployment):

```shell
uv run python scripts/fls_audit.py --baseline-deployment-offset 2
uv run python scripts/fls_audit.py --current-deployment-offset 1
```

## Snapshot workflows (text diffs)

Create a snapshot of the current FLS text:

```shell
uv run python scripts/fls_audit.py --write-text-snapshot build/fls_audit/snapshots
```

Compare against a prior snapshot:

```shell
uv run python scripts/fls_audit.py --baseline-text-snapshot build/fls_audit/snapshots/<snapshot>.json
```

## Offline audit

```shell
uv run python scripts/fls_audit.py --snapshot path/to/paragraph-ids.json
```

## Heuristics and legacy output

- Include heuristic match details:

```shell
uv run python scripts/fls_audit.py --include-heuristic-details
```

- Append the legacy diff section:

```shell
uv run python scripts/fls_audit.py --include-legacy-report
```

## Cache

The FLS repo and delta binaries are cached under `./.cache/fls-audit/` and are safe to delete.

## Rationalization checklist

1. Check if any guidelines are affected. If none, go to step 6.
2. For each affected guideline, audit the previous and current text of the
   referenced FLS paragraph.
3. If the prior and new text do not affect the guideline, continue to the next
   affected guideline.
4. If the text change affects the guideline, update the guideline to match the
   new FLS text.
5. Repeat until all affected guidelines are handled.
6. Done.

After completing the checklist, update the local `spec.lock`:

```shell
uv run --frozen make.py --update-spec-lock-file
```

Open a new PR with only the changes needed to rationalize the guidelines with
the updated FLS text. Include `Closes #<audit issue>` so the merged
synchronization closes the corresponding audit campaign.
