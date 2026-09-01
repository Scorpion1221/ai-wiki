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
audit is either `passed` or `needs_attention`. The latter is a valid, non-retryable outcome
that leaves an explicitly bounded durable concept unverified. A completed audit never leaves
a concept in the transient `draft` state: `passed` is stable and currently verified;
`needs_attention` is stable/deprecated but unverified. Only technical/validation/Git
failures produce a failed audit job. If ingest changed no concept files, audit returns an
immediate idempotent `passed` job with `reason: no_concepts_to_audit` and no audit commit.
The service also repairs reviewer bookkeeping slips deterministically: it restores the
pre-audit `sources` provenance, removes a generation refresh when no substantive change
survives, and promotes a leftover transient `draft` without adding verification; the job
records these under `deterministic_repairs`.
Repeating `audit` reuses an audit attempt while it is `queued`, `running`, or successfully
`done`. A `failed` attempt remains available for diagnosis, but a subsequent call creates
and queues a new attempt; callers must bound technical retries.

Maintenance checkpoints use the durable audit Job as their receipt: `done`, successful
validation, `passed`/`needs_attention`, and the successful Git result when applicable.
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
- `ai-wiki-maintainer` — `ingest → poll → audit → poll → checkpoint` orchestration;
- `okf-knowledge-curator` — strict OKF v0.2 authoring protocol used by the worker.

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
check before enabling its Maintainer automation.

The CLI is non-interactive: usage/API failures are structured on stdout with exit code 2/1, and destructive `bundle rm` requires `--yes`. Bare `-v`, `-V`, and `--version` probes return only the version.

## Configuration (env)

| Var | Meaning |
|-----|---------|
| `AIWIKI_BUNDLES` | dir holding one bundle per subdirectory; each writable bundle owns its Git repo |
| `AIWIKI_BUNDLE` | a single bundle dir; it must be the Git repo root when writes are enabled |
| `AIWIKI_DEFAULT_BUNDLE` | bundle used when a request omits `?bundle=` (optional) |
| `AIWIKI_TOKEN` | bearer token clients must present |
| `AIWIKI_PORT` | service port (default 8787) |
| `AIWIKI_DISABLE` | comma-list of endpoints to 403 (e.g. `ingest,audit,create,delete,search,grep`) |
| `AIWIKI_CURATE` | `auto` (default) or `off` to disable the curation trigger |
| `AIWIKI_AGENT_BIN` | Codex executable (default `codex`) |
| `AIWIKI_AGENT_MODEL` | explicit curator/auditor model (default `gpt-5.6-sol`) |
| `AIWIKI_AGENT_REASONING_EFFORT` | explicit reasoning effort (default `high`) |

Requires Python ≥ 3.11. Licensed under Apache-2.0 (see LICENSE / NOTICE).
