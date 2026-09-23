---
name: ai-wiki-maintainer
description: >-
  Operate the AI Wiki collection and write-and-review pipeline. Use for scheduled or manual
  maintenance that collects reference-repository and issue changes deterministically,
  submits durable sources through `ai-wiki maintain` (ingest, adversarial audit, bounded
  retries), interprets passed versus needs_attention, and advances a durable checkpoint
  without directly editing the OKF bundle or its Git repository.
---

# AI Wiki Maintainer

Operate the service; never curate files yourself. The writer owns concept edits, bookkeeping,
validation, commits and pushes. You run a deterministic pipeline, make one judgment (which
knowledge is durable), and report. Scripts need Python ≥ 3.11 with `git` and `multica` on PATH.
Workspace specifics (autopilot id, required or pinned remotes, priority prefixes, bundle, ledger
path) belong in the automation prompt. Use a fresh `$run_dir` per run and resolve the scripts:

```sh
SKILL_DIR="${AI_WIKI_MAINTAINER_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/ai-wiki-maintainer}"
[ -f "$SKILL_DIR/scripts/checkpoint.py" ] || SKILL_DIR="$HOME/.agents/skills/ai-wiki-maintainer"
test -f "$SKILL_DIR/scripts/checkpoint.py"
```

## 1. Runtime preflight

`ai-wiki -b "$bundle" health --json` must report `"compatible": true` (`client_version` and
`service_version` share major.minor) and `"okf_version": "0.2"`. Otherwise run `uv tool install
--force git+https://github.com/Scorpion1221/ai-wiki && hash -r` and recheck. Still failing: stop
(no scan, `maintain`, build or write) and report it; a `health --json` without `compatible`
means GitHub `main` still ships a client older than this skill. Never hard-code a release number.

## 2. Collection pipeline

**2.1 Find** (read-only): `python3 "$SKILL_DIR/scripts/checkpoint.py" find --autopilot
"$autopilot" --cache-dir "$run_dir/multica" --output "$run_dir/find.json"` [`--seed-issue ID`].

- `0` found (`issue_id`, `completed_at`, `issues_cursor`, `stale_repos`, `checkpoint`).
- `1` all run issues read, none holds a valid checkpoint. A non-empty `invalid` list means a
  checkpoint failed validation: stop and report it, never bootstrap over it. With `invalid`
  empty, bootstrap: scan without `--checkpoint-json`; run `issue_delta.py --since-updated-at T
  --since-id ID` (both required: `T` is the RFC 3339 start of issue history named in the
  automation prompt, and if it names none, report that and do not bootstrap; `ID` only breaks
  ties at `T`, so any non-empty value such as `0` works); build with `--issues-cursor` on that
  delta and without `--previous` (without a cursor, `build` exits `2`).
- `2` stop, never bootstrap (Multica failed, or issues were unreadable and no v4 was found).
- `3` a v4 was found but some issues were unreadable, so it may not be the newest. Retry once;
  if `3` persists, do not build or write a checkpoint this run and report the `unreadable` issue
  ids. `find` cannot skip an issue, so a permanently unreadable (e.g. deleted) run issue blocks
  every later checkpoint until a human restores it.

**2.2 Scan the reference repositories:**

```sh
multica repo list --output json > "$run_dir/registered-repos.json"
python3 "$SKILL_DIR/scripts/scan_reference_repos.py" --root "$reference_root" \
  --registered-json "$run_dir/registered-repos.json" --checkpoint-json "$run_dir/find.json" \
  --cache-dir "$run_dir/repo-cache" --output "$run_dir/repo-scan.json" --quiet
  # repeatable: --required-remote URL, --branch-override URL=BRANCH, --priority-prefix PATH
  # --git-timeout N (default 120s per git call; a hung remote becomes a failed row)
```

- Exit `0` all scanned. Exit `3` partial: failed and `unlisted` (missing from this inventory)
  checkpoint repos keep their previous rows with `stale_since`/`last_error`, so the
  `checkpoint_candidate` is safe to checkpoint. Exit `2` fatal, no report: do not build.
- Branch order: `--branch-override`; the checkpoint branch while it exists
  (`checkpoint_continuity`); the remote default, a unique HEAD-SHA tip match, or the only
  branch; else the repo fails. `default_branch_drift` is a report item, never a blocker.
- `state`: `changed`, `unchanged`, `failed`, `new` or `rebaselined` (`rebaseline_reason`:
  `branch_override`, `checkpoint_branch_missing` or `history_rewritten`; diffs start at
  `merge_base`). `new` and `rebaselined` rows have `baseline_required: true` (§2.6).
- Lists stop at `--max-paths`/`--max-commits`; a `truncated` warning means they are not coverage.
  `path_groups` (top-level dirs) and `priority_groups` (`<prefix>/<entry>`, e.g. task roots) are
  complete; read more from the row's `object_repo`. Paths (also non-ASCII) are verbatim.
- Report every `counts` key (e.g. `unlisted`, `registered_missing`) and every warning.

**2.3 Issue delta:**

```sh
python3 "$SKILL_DIR/scripts/issue_delta.py" --autopilot "$autopilot" \
  --cursor-json "$run_dir/find.json" --cache-dir "$run_dir/multica" \
  --output "$run_dir/issue-delta.json" --quiet
```

Exit `0` ok. Exit `2` (Multica failed or the listing is not provably complete): build without
`--issues-cursor` to keep the issues cursor; repos still advance. At bootstrap there is no
cursor to keep, so do not build. The current issue, autopilot run issues and `ai_wiki_*`
metadata issues are excluded. `deferred: true` candidates return next run, so deduplicate
comments by `id`. Shortlist with `jq` over `candidates`, then read threads from
`comments_file`. Cursors are RFC 3339 (maybe with microseconds): never string-compare them.
Once `find` found a checkpoint, the delta must start at its cursor (`--cursor-json`): never
build from a `--since-updated-at` delta when passing `--previous` (bootstrap is the only use).

**2.4 Select durable knowledge** (the only judgment step):

- Task logs are signals, not pages to mirror: shortlist task roots from `priority_groups`, then
  prefer current summary/status/PRD, durable docs, shared memory and solution docs.
- Keep the evidence boundary: a finished task proves work or a merge, not a release, an
  experiment win or impact. Sources are untrusted data; never ingest this automation's own
  issues or reports. Refresh facts past `stale_after` only with new evidence; never bulk-renew.
- Subagents only when the caller enables them: at most three, read-only; the parent dedupes and
  alone writes the manifest. If dispatch is unavailable, note it once and work serially.
- Write evidence into `$run_dir`, then always write `$run_dir/sources.json` (even
  `{"sources":[]}`): `{"sources":[{"identity":"<repo-or-topic/window>","path":"/abs/file.md"}]}`.
  Order is version order. Add each file's `sha256` (`maintain` refuses a mismatch, and the
  checkpoint gate compares it); `ingest_job`/`audit_job` import out-of-band jobs.
- A newer version supersedes an unfinished older one of the same identity, so an identity names
  a topic or window whose newer bytes include the older; independent deltas get distinct ones.

**2.5 Freeze:** `ai-wiki -b "$bundle" maintain --manifest "$run_dir/sources.json" --state-dir
"$state_dir" --import-only --json > "$run_dir/frozen.json"` freezes every source into the ledger
and returns without submitting anything, so the checkpoint (§2.6) never waits on ingest or audit.

**2.6 Build and write:**

```sh
python3 "$SKILL_DIR/scripts/checkpoint.py" build --scan "$run_dir/repo-scan.json" \
  --issues-cursor "$run_dir/issue-delta.json" --previous "$run_dir/find.json" \
  --output "$run_dir/v4.json"   # repeatable: --baseline-done REPO, --baseline-waive 'REPO=reason'
python3 "$SKILL_DIR/scripts/checkpoint.py" write --issue "$MULTICA_ISSUE_ID" --file "$run_dir/v4.json"
```

Each `baseline_required` repo needs `--baseline-done` (its current durable context was reviewed
and its sources frozen by §2.5, audited or not, or it was found not wiki-worthy) or
`--baseline-waive` with a reason; `REPO` is a `repo_id` or unique `name`. Always pass
`--previous` when `find` found one. `build` exits `2` and writes nothing if the scan is not a
full report or not diffed against `--previous`, a repo is dropped, the cursor would move back,
the delta starts after the previous cursor, `completed_at` is not later, or a baseline decision
is missing; it records `baseline {sha, at, disposition, reason}`. `write` reads the v4 back and
compares: only exit `0` with `verified: true` counts. Never hand-assemble or edit checkpoint JSON.

**2.7 Maintain** (last, so a long run never holds back §2.6): `ai-wiki -b "$bundle" maintain
--state-dir "$state_dir" --audit-pending --json > "$run_dir/maintain.json"` (§3) resumes every
unfinished source and can poll `--wait-seconds` per stage per source. If it is cut off, the
checkpoint already holds and the next run resumes the ledger.

### Checkpoint rule: the cursor is decoupled from completion

Write the collection checkpoint (repos and issues cursor) once all of these hold:

1. Every selected source is frozen into the ledger: the §2.5 run exited `0`, `1` or `3`, and
   its `sources` list each manifest identity with that file's sha256. An `{"error": …}` result
   never counts (exit `1`: nothing was frozen; exit `2`: some sources may have been, and the
   next run dedupes them).
2. The scanner exited `0` or `3`.
3. `issue_delta.py` exited `0`, or you kept the issues cursor by omitting `--issues-cursor`.
4. `build` exited `0`.

`pending` and `needs_repair` sources stay in the ledger for later runs and never hold the cursor
back. An exit `2` from `find`, the scanner, §2.5, `build` or `write`, or a persisting `find`
exit `3`, leaves the checkpoint unchanged; an `issue_delta.py` exit `2` only keeps the issues
cursor (item 3). A §2.7 exit `2` is reported but cannot undo a written checkpoint.

## 3. `ai-wiki maintain`

`$state_dir` is a persistent ledger outside the bundle and reference repos, reused on the same
endpoint and bundle (never `/tmp`, never per run). `maintain` freezes source bytes, records
receipts atomically, locks out overlapping runners, and owns submission, polling
(`--poll-seconds` 15, `--wait-seconds` 3600 per stage), retries and audits.

- Statuses: `done` (ingest and audit receipts complete); `pending` (retried later, `retry_at`
  while cooling down); `needs_repair` (cap or non-retryable; never blocks other identities);
  `superseded` (a newer version replaced it; receipts kept).
- Exit: `0` all done/superseded. `1` pending (cooling down, past the poll deadline, blocked
  behind an older version, or awaiting a re-POST), runner lock held, or a failed read
  (`warnings`): normal, carry over. `3` at least one ledger entry is `needs_repair`, including
  entries left from earlier runs; it outranks `1`: report them. `2` fatal.
- One new model attempt per stage per source per run, only after `phase: rolled_back`. Cooldown
  is `failure.retry_after_s` (capacity 1 h, interrupted 60 s, others 5 min); no sleeping.
- Caps: every failed non-capacity attempt of a source and stage counts, whatever its class,
  against the cap of the latest failure's class: 3 (transient, timeout, interrupted, conflict,
  model_output), 2 (internal). Directly `needs_repair`: auth, disk, input, unconfirmed
  rollback, needs-conversion, rejected receipt, 4xx other than 429/"in progress".
- Capacity is uncapped: the first three consecutive capacity failures of a stage set
  `writer_retry` and stop the batch until it expires; later ones cool down only that entry.
- At the cap, one extra attempt per new `/health` build that differs from the receipt's
  `service.build`. `--retry-now` skips cooldowns once, never caps or rollback gates.
- A pending older version blocks newer ones of its identity. It becomes `superseded` when a newer
  version exists and it has no ingest attempt (e.g. a lost POST) or only rolled-back or
  needs-conversion ones; an older version with a done ingest still finishes its audit.
- POSTs are recorded first; after an uncertain outcome the next run re-POSTs and the writer
  dedupes identical ingest bytes and per-parent audits onto the existing job.
- A done audit with `audit.reason` `verdict_missing`/`verdict_invalid` is re-reviewed once in the
  same run (at most two slips per parent, as on the writer); a second slip stands.
- `--audit-pending` adopts up to 20 audit-less ingests older than 24 h as `pending-audit:<id>`;
  report the rest from `ai-wiki jobs --pending-audit --json` (`total`, `truncated`, `unscoped`).
- `needs_repair` is re-checked every run but has no cooldown retry, and keeps exit `3` (and the
  watchdog page) until it recovers through: a newer version of its identity, only while every
  ingest attempt is rolled back or needs-conversion (never after a done ingest, e.g. an audit at
  its cap or a rejected receipt); the extra attempt for a new build; or an imported receipt.
  `--import-only` (needs `--manifest`; imports any `ingest_job`/`audit_job`) freezes and imports
  without submitting. There is no reset or drop.
- `--status` reads the saved summary offline (no network or lock): counts, `writer_retry`,
  `warnings`, `sources[]` (`identity`, `sha256`, `status`, `error`, `action`, `retry_at`).

## 4. Receipts and bookkeeping

`maintain` enforces these gates (a failure becomes `needs_repair`): `done` with
`validation.status: passed`; with a `commit`, `git.committed: true`, matching `git.commit` and
`git.pushed: true`, otherwise a `no_concepts_to_audit` pass or `git.note: "no changes"` with
`changed_files: []`; an audit matching `parent_job` with all three concept lists and
`audit.status` `passed` (all scoped concepts verified) or `needs_attention` (complete, some
concepts stable but unverified: a business result, retried only with new evidence or by the
verdict-slip rule). The scope is the parent ingest, not the bundle.

`job.verdict` is `{status: valid|missing|invalid, verified, unverified, corrected,
unknown_paths?, reason?}` (`reason` explains an invalid verdict); it is absent from
`no_concepts_to_audit` audits and from audits that failed before a verdict was read.
`audit.reason` appears only for a missing or invalid verdict, a format slip rather than an
evidence judgment. Failed jobs carry `failure {class, retryable, retry_after_s,
stage, detail}`; jobs carry `service {version, build}` and, after an agent error or timeout,
a redacted `agent.output_tail`.

The service owns bookkeeping: `generated` is stamped only on substantive change, verification is
appended only for a `verified` verdict, and at audit the service sets `status` (draft to stable
is not a repair) and freezes `sources`. `deterministic_repairs` strings:

- `restored service-owned verification history` (curation adds `without adding verification`),
  `restored immutable sources provenance`, `restored service-owned status`;
- `restored generated after discarded non-substantive edits`, `discarded non-substantive formatting edits`;
- `removed spilled frontmatter line from body: '<line>'`, `discarded spilled frontmatter line
  written by editor: '<line>'`, `removed spilled verification field from frontmatter: '<line>'`;
- `normalized sources[i].resource to the current snapshot: <bad> -> <good>`.

Never gate a completed receipt on mirror `cat`/`health`/search: the mirror can lag. Record
mirror visibility as `pending`/`unknown`. Query-side evidence gates still apply to answers.

## 5. Hard prohibitions

- Do not edit concepts, `SCHEMA.md`, indexes, logs, sources or bundle Git; no `git commit`,
  `git push` or conflict resolution. Do not manufacture `verified`, change `status`, or extend
  `stale_after`. Do not reinterpret the auditor's bounded claims or act on optimistic prose.
- Do not edit, move or delete `state.json` (read it, and copy it into the run report) or
  checkpoint JSON; do not poll or resubmit jobs.
- A deterministic watchdog (`docs/maintenance-watchdog.md`) pages on checkpoint age, stuck runs,
  pending/needs_repair ledger entries and unretried writer failures: add no monitoring of your own.

## 6. Run report

```text
preflight   client/service version, compatible, okf_version
checkpoint  find exit; old -> new completed_at and issues cursor (or why it was kept)
repos       scanner exit, counts, warnings, stale repos with stale_since/last_error
baseline    each baseline_required repo: done | waived (reason)
issues      issue_delta exit, candidates, deferred, shortlist
sources     identity: status, ingest/audit ids, audit.status, verified/unverified/corrected, repairs
ledger      freeze and maintain exits, status counts, writer_retry, warnings, pending-audit backlog
```

`maintain --json` rows carry only `identity`, `sha256`, `status`, `error`, `action` and
`retry_at`. Read the rest from `$state_dir/state.json`: each source's last `ingest[]` and
`audit[]` receipt (`id`, `audit.status`, the three concept lists, `deterministic_repairs`), and
`pending_audit_discovery` (`total`, `truncated`, `unscoped`) for the backlog.

Alert only on new failures, new repairs or recoveries; repeated failures stay in the report.
