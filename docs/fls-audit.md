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
has moved beyond `src/spec.lock`. The scheduled Nightly workflow and tagged
release/deploy workflow enforce lock freshness and fail while it is stale.

Local builds enforce freshness by default. Use `--ignore-spec-lock-diff` only
when intentionally reproducing the nonblocking CI policy:

```shell
uv run --frozen make.py --ignore-spec-lock-diff
```

## Rolling audit issue

The scheduled FLS Audit workflow maintains one issue for each committed
`spec.lock` baseline. The issue body contains the latest cumulative report.
When the net drift changes, the bot adds one comment listing every newly
active, updated, and resolved drift item since its last successful update.

If scheduled runs are missed, the next run posts the complete net catch-up as
one comment. Changes that appeared and were fully reverted between successful
bot observations are intentionally not reconstructed.

The workflow is also available through `workflow_dispatch`. Operational manual
runs must select the repository's default branch and are idempotent: rerunning
an unchanged audit does not edit the issue or add a comment.

Every required `build` check runs the focused FLS audit unit, integration, and
workflow contract suite before building the documentation. After each live
reconciliation, the bot also rereads GitHub and verifies the issue identity,
open or closed status, campaign state, complete comment sequence, and unique
batch markers. A failed postcondition leaves the workflow red instead of
silently accepting incomplete or duplicate audit history.

Maintainers normally close the audit issue from the synchronization PR by
including `Closes #<issue>` in its body. If no guideline updates are required,
a maintainer with triage permission may instead comment
`@guidelines-bot /accept-no-fls-changes`; that command reruns the audit and
refuses to proceed if any guideline is affected.

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
