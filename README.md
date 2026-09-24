# ai-wiki

A small **service + CLI** for serving and maintaining a strict
[OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(Open Knowledge Format) markdown knowledge bundle. This release intentionally does not
read or write legacy v0.1 concepts; migrate the whole bundle before upgrading the service.

Agents read the bundle like a filesystem — `ls` / `cat` / `grep` plus ranked,
CJK-aware search — over a token-authed HTTP API, so no one needs a full local clone.
They *maintain* it by **submitting a source**: a headless-agent curation pass folds the
source into the bundle as probationary concepts, flags contradictions, and runs the
deterministic close-out. Reads stay deterministic (no LLM in the service); only curation
and adversarial audit use agents.

## Design

- **Engine** (`src/aiwiki/engine/`) — deterministic OKF maintenance: validate, source-drift
  detection, index generation, link/health lint, and update invariants (array-union,
  identity-lock, body-shrink guard). PyYAML + stdlib only; no LLM, no network.
- **Service** (`src/aiwiki/service/`) — FastAPI read API (`health/ls/cat/grep/search/log`)
  + write/review path (`POST /ingest`, `POST /jobs/{ingest_job_id}/audit`, `GET /jobs/{id}`).
  One server hosts **many bundles** under
  a single URL: list them with `GET /bundles`, pick one per request with `?bundle=<name>`,
  create/delete with `POST`/`DELETE /bundles`. Bearer-token auth, path sandboxing, and an
  `AIWIKI_DISABLE` switch for read-only / drill-only deployments.
- **CLI** (`src/aiwiki/cli/`) — `ai-wiki`, a thin stdlib-only, agent-first client. Its no-args home view
  shows live bundle context; structured output is compact TOON (with `--json` escape hatches), while
  `cat` stays raw Markdown.
- **Runtime** (`src/aiwiki/runtime/`) — triggers headless curation and adversarial-audit
  passes. These are the only LLM-using parts; disable them for a pure read deploy.

### Read/write split (multi-writer)

A public **read-only mirror** (`AIWIKI_DISABLE=ingest,audit,create,delete`) and a team **ingest
worker** (curation enabled, with `codex` + a writable git remote) can be two deployments
of the same service — and behind one URL via path-routing (`/ingest`,`/jobs` → worker,
reads → mirror). `POST /ingest` takes pasted `text` or any file (`content_b64`+`filename`),
stored verbatim in `sources/inbox/`. Sources Codex can read (text/code/image) are queued;
PDF and other opaque types are stored as `needs-conversion` until an explicit converter is
provided. A single serial worker drains the
queue one job at a time (so concurrent submissions never race on the bundle/git) — it also
sweeps the inbox on a timer to pick up out-of-band drops — rebases onto the remote before
curating, independently validates the result, then commits and pushes only if validation
passes. Re-submitting identical content returns the existing non-failed job as a successful
no-op; failed jobs can be retried. `GET /jobs/{id}` reports validation, commit,
and changed files. On a rejected push it rebases onto the moved remote and
retries only when Git can rebase cleanly; a real conflict or final push failure marks the
job failed, rolls back, and restores the inbox source for a retry from latest remote state.
No second LLM pass mutates already-validated content. If the bundle repository already tracks a root
`viz.html`, successful curation refreshes that snapshot before the same commit; repositories
without one remain unchanged. The mirror pulls the result.

The service—not Codex—writes the byte-identical immutable `sources/` snapshot. Curation
runs in a disposable copy that contains no `.git`, `.okf`, or `sources/inbox`; only concept
bytes that pass scope, provenance, policy, and full-bundle validation are applied to the
live transaction. Curator and auditor use an explicit model/reasoning setting, record it
plus heartbeat timestamps in the job, and run with network/apps/plugins/memory disabled.

The writer durably records its Git base, branch, phase, commit, and the ignored inbox bytes
before an agent can mutate the bundle. After a service restart it aborts any interrupted
rebase and either (a) preserves a job commit already present on the remote and completes a
fully recorded result, or (b) resets the unpublished transaction to its exact base and
restores the inbox source. Recovery never resets a different checked-out branch, and never
marks an audit successful without its durable structured audit result.

For write deployments, each bundle must be the root of its own Git repository. This keeps
rollback and recovery scoped to one knowledge base. `bundle create` always initializes that
dedicated repository, even when `AIWIKI_BUNDLES` itself lives inside another checkout.
Read-only deployments may still serve bundles from repository subdirectories.

## Quick start

```bash
uv sync --extra service --extra dev
uv run ai-wiki config set --endpoint http://127.0.0.1:8787 --token "$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
AIWIKI_BUNDLES=./bundles ./run-local.sh      # serve a dir of bundles on :8787 (token from CLI config)

ai-wiki                    # live bundle overview + directories + next commands
ai-wiki bundle list        # bundles hosted on the server (active/default state)
ai-wiki bundle use <name>  # switch the active bundle (or `bundle create <name>`)
ai-wiki health
ai-wiki ls                 # list a level; TOON separates concepts from structural entries
ai-wiki cat <path>         # raw Markdown preview; add --full only if truncated
ai-wiki cat <path> --json  # path + content + derived OKF metadata
ai-wiki search "<query>"
ai-wiki log --tail 30      # newest change-ledger lines first
ai-wiki ingest notes.md    # submit a source for curation (needs `codex` + AIWIKI_CURATE!=off)
ai-wiki jobs <ingest-job-id>
ai-wiki audit <ingest-job-id>  # adversarial review of a completed ingest; returns an audit job
ai-wiki jobs <audit-job-id>
okf-render-viz <bundle> [out.html]  # generate a local HTML knowledge-graph snapshot
```

Engine CLIs are exposed as `okf-validate`, `okf-scan-sources`, `okf-lint`, etc.

Search is deterministic and explainable: it normalizes separators, applies Latin word
boundaries plus CJK bigrams, searches path/title/aliases/tags/description/body across every
concept, and returns `match.phrase`, `match.coverage`, `match.fields`, and `match.terms`.
Exact phrases and complete query coverage outrank repeated partial tokens.
TOON list output keeps concept evidence fields off directories and structural files. JSON
search/grep/log output includes `shown`, `total`, and `truncated` so callers cannot mistake a
bounded response for a complete result set.

An ingest is not verification. New/changed concepts are audited separately; a completed
audit is either `passed` or `needs_attention`. The latter is a valid outcome that leaves an
explicitly bounded durable concept unverified; it is not retried without new evidence, except
the single re-review after a verdict slip described below. A completed audit never leaves
a concept in the transient `draft` state: `passed` is stable and currently verified;
`needs_attention` is stable/deprecated but unverified. Only technical/validation/Git
failures produce a failed audit job. If ingest changed no concept files, audit returns an
immediate idempotent `passed` job with `reason: no_concepts_to_audit` and no audit commit.
Bookkeeping is service-owned. Agents decide content only: the service restores `verified`
history (and, at audit, `status` and `sources`), stamps `generated` with trusted time only for a
substantive change, appends a verification event only when the auditor's JSON verdict lists the
concept as `verified`, and sets `status` to `stable` at audit (`deprecated` stays). Formatting-only
edits and frontmatter spilled into a body are discarded; each repair is recorded under
`deterministic_repairs`. An audit whose verdict is missing or unparseable ends `needs_attention`
with `audit.reason: verdict_missing`/`verdict_invalid`, and one more audit of that ingest is
allowed.
Repeating `audit` reuses an audit attempt while it is `queued`, `running`, or successfully
`done`, except that the first `done` attempt with a verdict slip is not reused: the next call
queues the one allowed re-review. A `failed` attempt remains available for diagnosis, but a
subsequent call creates and queues a new attempt; callers must bound technical retries.

A source's durable audit Job is its completion receipt: `done`, successful validation,
`passed`/`needs_attention`, and the successful Git result when applicable. The daily collection
cursor is separate: it advances once selected sources are frozen into the `maintain` ledger.
The worker already enforces concept status and verification before committing. Do not
repeat that gate against live `cat`/`health` results: read mirrors can lag, and later ingests
can change the same concept. Report mirror visibility separately as a warning; it must not
block completed source checkpoints or trigger duplicate curation. This does not relax the
read-side evidence gates for answering current-fact questions.

Pasted/raw Markdown evidence is stored as `sources/*.md.source`, not `*.md`, so it cannot
be mistaken for an OKF concept. `SCHEMA.md` and `purpose.md` remain discoverable structural
documents with `type: Contract` frontmatter.

`okf-validate <bundle>` checks both official v0.2 conformance and the stricter AI Wiki
profile. Use `--conformance-only` only when testing third-party interoperability.

## Skills are source-controlled

The canonical query, maintenance, and curation skills live in [`skills/`](skills/):

- `ai-wiki` — read-side status/trust/freshness gates;
- `ai-wiki-maintainer` — deterministic collection (`checkpoint.py`, `scan_reference_repos.py`,
  `issue_delta.py`), the `ai-wiki maintain` ledger, and checkpoint orchestration;
- `ai-wiki-curating-maintainer` — the maintainer that curates: `doctor`, `maint begin/next`,
  local curation, `validate`/`propose` through the writer's gate, and the `maint end` report;
- `okf-knowledge-curator` — strict OKF v0.2 authoring protocol used by the worker and, in its
  remote maintainer mode, by the curating maintainer.

Check an installed runtime for drift, then explicitly synchronize it:

```bash
python3 scripts/sync_skills.py --check
python3 scripts/sync_skills.py --apply
# alternate runtime root:
python3 scripts/sync_skills.py --check --dest /path/to/.agents/skills
```

The sync preserves a platform-managed `multica-metadata.json` but replaces every other
file in these skill directories, so stale bundled scripts cannot silently override the
repository version. Publish the same directories to Multica and compare them against this
check before enabling its Maintainer automation. Sync `ai-wiki-maintainer` only after its
release is on GitHub `main` and deployed to the writer and read mirror: its preflight needs
`compatible` from `ai-wiki health --json`, and reinstalling an older client cannot supply it.

The CLI is non-interactive: usage/API failures are structured on stdout with exit code 2/1, and destructive `bundle rm` requires `--yes`. Bare `-v`, `-V`, and `--version` probes return only the version.

## Configuration

### Optional Codex subscription / API-wrapper selection

Both ingest and audit use the same Codex-compatible executable. On the **worker host**, merge
an optional `agent` object into `~/.ai-wiki/config.json` (or the file selected by
`AIWIKI_CONFIG`), preserving the existing connection/token/bundle fields:

```json
{
  "agent": {
    "bin": "codex",
    "model": "gpt-5.6-sol",
    "reasoning_effort": "high"
  }
}
```

This is also the default when `agent` is absent: use the worker user's existing Codex login
(for example, a ChatGPT subscription), without changing its account or config. Authentication
is delegated to Codex; this selection does not itself log in or guarantee a subscription.
To use an existing API wrapper instead, replace only the `agent` object:

```json
{
  "agent": {
    "bin": "/root/.local/bin/codex-9router",
    "model": "gpt-6-astra-combos",
    "reasoning_effort": "xhigh"
  }
}
```

- Precedence per field: `AIWIKI_AGENT_*` environment variable → `agent` field → built-in default.
  Remove old environment overrides when switching via the file.
- Settings are loaded at worker startup; an existing service must be restarted separately
  to pick up changes. Editing a remote client's config does **not** reconfigure the server.
- The executable is one path or PATH name, **not** a shell command. Put provider flags and
  credential loading in the wrapper. Set model/effort here explicitly: worker arguments
  override wrapper defaults. Missing/broken wrappers never fall back to a different account.
- A missing default config preserves env-only deployments. An explicitly selected missing
  config, invalid JSON, or invalid/unknown `agent` fields fails startup rather than silently
  choosing defaults. The supported fields are:

  | `agent` field | Environment override | Default | Meaning |
  |---|---|---|---|
  | `bin` | `AIWIKI_AGENT_BIN` | `codex` | Codex executable or compatible wrapper |
  | `model` | `AIWIKI_AGENT_MODEL` | `gpt-5.6-sol` | curator and auditor model |
  | `reasoning_effort` | `AIWIKI_AGENT_REASONING_EFFORT` | `high` | curator and auditor effort |
  | `timeout_s` | `AIWIKI_AGENT_TIMEOUT_S` | `1500` | curation pass wall clock, seconds |
  | `audit_timeout_s` | `AIWIKI_AGENT_AUDIT_TIMEOUT_S` | `1200` | adversarial audit wall clock, seconds |
  | `repair_timeout_s` | `AIWIKI_AGENT_REPAIR_TIMEOUT_S` | `600` | bounded curation repair pass, seconds |

  Timeouts must be integers from 60 to 7200; any other value stops the worker at startup.
  One ingest's agent passes can take up to `timeout_s + repair_timeout_s` (2100 s by default),
  within the `maintain` default `--wait-seconds 3600`. That wait also counts time queued behind
  other jobs on the serial worker and Git/validation time, so it can run out; the source then
  stays `pending` ("poll deadline reached") and the next run resumes the same job.
- `ai-wiki config set` and `bundle` selection preserve `agent`. `config show` remains a
  connection-only view. On a writer, `ai-wiki health --json` exposes the **running server's**
  `writer_agent` (`runtime`, `bin`, `model`, `reasoning_effort`); jobs record the same settings.
- Never put provider API keys here. Keep them in the wrapper's external secret file (mode
  `600`); keep the wrapper mode `700`. AI Wiki does not copy `auth.json`, replace `CODEX_HOME`,
  or rewrite normal Codex model/provider/auth settings. Codex itself may still record project
  trust in its config; see the smoke-test isolation notes in the integration guide.
- Selecting a backend does not grant permission to ingest/audit or enable a schedule.
  Curation/audit retain the existing isolated workspace, no tool-network access, validation,
  service-owned Git closeout, and rollback boundaries.

See [Codex integration and read-only SSH testing](docs/codex-integration.md) for the wrapper
contract, credential boundaries, and a separate read-only client workflow.

### Environment variables

| Var | Meaning |
|-----|---------|
| `AIWIKI_BUNDLES` | dir holding one bundle per subdirectory; each writable bundle owns its Git repo |
| `AIWIKI_BUNDLE` | a single bundle dir; it must be the Git repo root when writes are enabled |
| `AIWIKI_DEFAULT_BUNDLE` | bundle used when a request omits `?bundle=` (optional) |
| `AIWIKI_TOKEN` | bearer token clients must present |
| `AIWIKI_PORT` | service port (default 8787) |
| `AIWIKI_DISABLE` | comma-list of endpoints to 403 (e.g. `ingest,audit,create,delete,search,grep`) |
| `AIWIKI_CURATE` | `auto` (default) or `off` to disable the curation trigger |
| `AIWIKI_CONFIG` | local client / worker JSON config (default `~/.ai-wiki/config.json`) |
| `AIWIKI_AGENT_BIN` | override `agent.bin`; Codex executable or compatible wrapper (default `codex`) |
| `AIWIKI_AGENT_MODEL` | override `agent.model` for curator and auditor (default `gpt-5.6-sol`) |
| `AIWIKI_AGENT_REASONING_EFFORT` | override `agent.reasoning_effort` (default `high`) |
| `AIWIKI_AGENT_TIMEOUT_S` / `AIWIKI_AGENT_AUDIT_TIMEOUT_S` / `AIWIKI_AGENT_REPAIR_TIMEOUT_S` | override the agent budgets (defaults 1500 / 1200 / 600 s) |
| `AIWIKI_BUILD_COMMIT` | deployed Git revision reported as `build` by `/health` and stamped on jobs |

Requires Python ≥ 3.11. Licensed under Apache-2.0 (see LICENSE / NOTICE).

### Build identity

`/health` (and `ai-wiki health --json`) reports `build`: `AIWIKI_BUILD_COMMIT` when set, else
the first line of `<app root>/.ai-wiki-deployed-revision`, else `null`. Jobs record the same
value as `service.build`. Set it on every deployment from the same revision, for example
`Environment=AIWIKI_BUILD_COMMIT=<sha>` on the worker unit, and for Docker:

```bash
docker build --build-arg AIWIKI_BUILD_COMMIT=$(git rev-parse HEAD) -t ai-wiki .
```

While `build` is `null`, `maintain` never grants its extra attempt for a newly deployed build.
`ai-wiki health --json` also adds `client_version` and `compatible` (client and service share
major.minor); maintenance automation fails closed when `compatible` is false.

## Maintenance recovery

`ai-wiki ingest --json` returns machine-readable submission IDs. `ai-wiki jobs
--pending-audit --json` discovers successful ingests older than 24 hours without an active
or completed audit. Use `ai-wiki maintain` with a persistent state directory:

```sh
ai-wiki -b my-kb maintain --manifest sources.json --state-dir ~/.ai-wiki/maintenance/my-kb --audit-pending
# Later scheduled runs resume saved work; omit the manifest to resume only.
ai-wiki -b my-kb maintain --state-dir ~/.ai-wiki/maintenance/my-kb
# Capacity restored early: skip the cooldowns once, never the caps or safety gates.
ai-wiki -b my-kb maintain --state-dir ~/.ai-wiki/maintenance/my-kb --retry-now --json
# Inspect the saved ledger offline (no network, no lock).
ai-wiki maintain --state-dir ~/.ai-wiki/maintenance/my-kb --status
```

The manifest is `{"sources":[{"identity":"<stable source identity>","path":"/absolute/evidence.md"}]}`.
Each run leaves every entry `done`, `pending`, `needs_repair`, or `superseded`, and exits:

| Exit | Meaning |
|---|---|
| `0` | every entry is done or superseded |
| `1` | work is pending (cooling down, past the poll deadline, blocked behind an older version, or awaiting a re-POST), the runner lock was held or the client config could not be read (an `{"error"}` result; nothing was frozen), or a read failed (listed in `warnings`); normal, resume later |
| `3` | at least one ledger entry is `needs_repair`, including entries left from earlier runs; outranks `1`; alert on it |
| `2` | usage or state error; fatal |

- Retries follow the failed job's `failure.class`. Each run makes at most one new attempt
  per stage per source, only after a confirmed rollback, and waits for `retry_after_s`
  (capacity 1 h, interrupted 60 s, others 5 min). Capacity failures stop the batch for their
  first three consecutive occurrences; after that only the entry cools down.
- Failed non-capacity attempts are counted per source and stage, whatever their class, against
  the cap of the latest failure's class: 3 for transient, timeout, interrupted, conflict, and
  model_output; 2 for internal (two transient failures then an internal one reach the cap).
  Past the cap, or on auth, disk, input, an unconfirmed rollback, or a rejected receipt, the
  entry becomes `needs_repair`, which never blocks other sources. One extra attempt is allowed
  for each new `/health` build that differs from the failed receipt's `service.build`.
- A newer version of the same identity supersedes an older unfinished one that has no ingest
  attempt or only rolled-back or needs-conversion attempts; its receipts are kept. Give
  independent deltas distinct identities.
- Each POST is recorded before it is sent. After an uncertain outcome the next run re-POSTs,
  and the writer dedupes identical ingest bytes and per-parent audits onto the existing job.
  A done audit with `audit.reason: verdict_missing`/`verdict_invalid` is re-reviewed once.
- `--import-only` (needs `--manifest`) freezes the sources and imports any `ingest_job`/
  `audit_job` created out of band without submitting anything, so a collection checkpoint can
  advance before a long run. Never edit `state.json`. A `needs_repair` entry is re-checked
  every run and recovers through a newer version (only while its ingest attempts are all
  rolled back or needs-conversion), the extra attempt for a new build, or an imported receipt;
  until then every run exits `3`. An operator (not the agent) can abandon one for good with
  `--drop SHA256_PREFIX --reason TEXT`: its status becomes `dropped`, receipts are kept, and it
  no longer affects the exit code.

The JSON summary carries status counts, `writer_retry`, `warnings`, and per-source `identity`,
`sha256`, `status`, `action`, `error`, and `retry_at`; job IDs, full receipts and the
pending-audit discovery counts stay in `<state-dir>/state.json`. Existing v1 state and exact
source bytes are reused; the Skill script is only a CLI forwarding entry point.

### Job receipts

`GET /jobs/{id}` (`ai-wiki jobs <id> --json`) records validation, commit, changed files, and
`deterministic_repairs`, plus:

- `failure {class, retryable, retry_after_s, stage, detail}` on a failed job. `class` is one
  of `capacity`, `transient`, `timeout`, `interrupted`, `model_output`, `conflict`, `auth`,
  `disk`, `input`, or `internal`; `detail` is redacted.
- `service {version, build}`: the writer that produced the receipt.
- `agent.output_tail`: the redacted last 4000 characters of the agent's output after a
  nonzero exit or timeout.
- On audits that ran a reviewer, `verdict {status: valid|missing|invalid, verified,
  unverified, corrected}` plus `unknown_paths` (paths outside the scope) and `reason` (on an
  invalid verdict) when present, and, for a missing or invalid verdict, `audit.reason`.
  `no_concepts_to_audit` audits and audits that failed before a verdict was read have none.

Receipts written before these fields existed are still classified by the client's legacy
rules.

### Watchdog

[`docs/maintenance-watchdog.md`](docs/maintenance-watchdog.md) describes
`scripts/maintenance_watchdog.py`, a read-only, LLM-free check (Python 3.11+ on the host where
it is installed) that pages Feishu about a stale checkpoint, stuck issues and runs,
`needs_repair` (`ledger_needs_repair`) or long-pending ledger entries, and writer job failures
that have no later attempt (`job_failed`, for up to 7 days).

## Development and CI

```bash
uv run --extra dev ruff check
uv run --extra dev --extra service pytest -q
```

GitHub Actions (`.github/workflows/ci.yml`) runs both on every pull request and on pushes to
`main`, on Python 3.11 and 3.12. Wait for green CI before merging, including hotfixes.
