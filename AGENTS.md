# Using ai-wiki (agent guide)

`ai-wiki` is a deterministic read window onto a curated **OKF v0.2 knowledge bundle**
served over HTTP. The server runs no LLM at all: reads are deterministic, and writes pass a
deterministic gate. Curation and adversarial audit are done by external agents that hold a
credential of that role (docs/external-agents.md). This guide is how an agent installs the
CLI, connects, reads safely, and submits source evidence.

## 1. Install the CLI

```bash
uv tool install git+https://github.com/Scorpion1221/ai-wiki
# or: pipx install git+https://github.com/Scorpion1221/ai-wiki
# or: git clone … && cd ai-wiki && uv run ai-wiki …
```

The client config lives at `~/.ai-wiki/config.json`.

## 2. Connect and choose a bundle

One server (URL + token) can host many knowledge bundles:

```bash
ai-wiki config set --endpoint https://<host>/ --token <token>
ai-wiki bundle list
ai-wiki bundle use solvely-web
ai-wiki bundle create my-kb
ai-wiki bundle rm my-kb --yes
ai-wiki -b other search "..."       # one-command override
```

If the service has a default or only one bundle, `bundle use` is optional.

## 3. Read it like a filesystem

```bash
ai-wiki                              # live bundle overview + next commands
ai-wiki health                       # OKF version, revision, type/status/trust/freshness
ai-wiki cat SCHEMA.md                # ORIENT FIRST: taxonomy + quality contract
ai-wiki cat purpose.md               # bundle scope and purpose
ai-wiki ls [<dir>]                   # browse progressively
ai-wiki ls -R                        # all concepts; -a includes dotfiles
ai-wiki search "<query>"             # ranked, CJK-aware discovery
ai-wiki grep "<pattern>" [--fixed]   # regex/literal; --limit 0 for all
ai-wiki cat <dir>/<name>.md           # preview; --full only if truncated
ai-wiki cat <dir>/<name>.md --json    # {path, content, metadata} for mechanical gates
ai-wiki links <dir>/<name>.md         # outbound + inbound relationships
ai-wiki log                           # newest change-ledger lines first
```

Orient through `SCHEMA.md`/`purpose.md`, then drill or search. Follow one relationship hop
when it can change the answer, especially experiment ↔ metric ↔ decision/risk.

TOON `ls` output separates semantic `concepts` from structural `entries`, so directories and
root documents do not pretend to have status/trust fields. `ls --json` remains the complete
raw entry array. JSON `search`/`grep`/`log` output is an envelope containing the rows plus
`shown`, `total`, and `truncated`; inspect those counts before treating a result as complete.

Structured results expose `status`, derived `trust` and `freshness`, `generated_at`,
`verified_at`, and `verification_current`. Trust follows OKF §5.3 across all verification
history; `verification_current` separately says whether an event confirms the current
generated revision. For current-fact decisions, treat `verification_current: false` like
unverified regardless of the historical trust tier. Search returns explainable `phrase`,
`coverage`, `fields`, and `terms`; exact phrases and full query coverage rank ahead of
repeated partial tokens. Trust/freshness remain tie-breakers. Run one search first and
rewrite it at most once only when results are empty or coverage is partial; apply this
current-fact gate after retrieval even when a weak hit ranks highly:

```text
stable + fresh + human-reviewed
stable + fresh + machine-confirmed
stable + fresh + unverified       (explicit caveat)
draft                              (transient pre-audit process/context only)
stale or deprecated               (history only)
```

Missing `stale_after` means freshness is unspecified, not fresh. For commercial metrics,
experiment winners, and released/live claims, fail closed unless the concept is stable,
fresh, `verification_current: true`, and backed by a source that proves that exact boundary.

**Discipline:**

- Cite the concept paths used, for example `metrics/<name>.md`.
- Trust only returned content; never invent metrics, prices, events, fields, dates, or outcomes.
- Surface material status/trust/freshness and the latest verification date.
- Follow `contested`, contradictions, and correction notes rather than choosing silently.
- This client targets OKF v0.2 only. Do not produce `timestamp`, string `sources`,
  `last_verified_at`, `# Citations`, or legacy statuses.

## 4. Write (when writes are enabled)

Agents never edit concepts or the bundle Git repository directly. The writer is a
deterministic gate (validation, service-owned bookkeeping, commit, push) that runs no LLM
(`AIWIKI_LLM=off`). Every write is a submission or a changeset from a principal of the right
role, onboarded as docs/external-agents.md describes.

**Members submit sources:**

```bash
ai-wiki ingest notes.md
ai-wiki ingest a.md config.json chart.png
cat notes.md | ai-wiki ingest - --title "<stable source identity>"
ai-wiki jobs <job-id>
```

Each submission becomes a work item in the maintainer's queue (`AIWIKI_INTAKE=inbox`), ahead
of repository and conversation items, and is committed to the wiki's Git at once: before it
answers, the writer commits the submission's copy to `sources/inbox/intake/` in a commit
`intake: <title> (<principal>)` and pushes it. Text is redacted of secrets first, as all
evidence is; images and PDFs are committed as sent, so never submit a file that holds a
secret. The answer and `ai-wiki jobs <id>` show that commit under `intake`; when the push
failed, `intake` says so, the item still waits for the maintainer, and the writer retries the
commit on its own. A link sent alone has nothing to commit until the maintainer reads it. The
maintainer runs once a day at 04:00 CST, so a submission waits at most about a day to be
curated; `ai-wiki jobs <id>` follows the item until a changeset curates it (with its commit)
or the maintainer skips it (with the reason). PDF and other opaque formats stay
`needs-conversion` rather than being guessed. Identical submissions are idempotent: a resend
makes no second commit.

**The maintainer curates.** One maintainer run per bundle at a time (the server's lease)
follows `skills/ai-wiki-curating-maintainer`: `maint begin` collects repositories and Multica
conversations deterministically and freezes their evidence on the writer, `maint next` claims
an item, the agent edits concepts only in a workspace pulled with `ai-wiki workspace pull`,
`ai-wiki validate` runs the gate's own code locally, and `ai-wiki propose` submits the
changeset, which the writer judges synchronously: committed and pushed, or refused with codes
to fix. The gate stamps `generated`, `verified` and `status`: curation never verifies. The
run's comment opens with the verbatim `maint end` report.

**The auditor verifies.** A separate agent, with its own `audit` principal, another OS user
and preferably another model family, reviews the audit backlog the server derives and
submits audit changesets; only those confirm a generation (machine-confirmed). No principal
may both curate and audit, and an auditor never reviews its own output.

Progress lives on the writer: work items, collector cursors and changeset receipts. A cursor
advances once its evidence is frozen, never waiting for an audit; audits trail on their own
schedule. A changeset's job is its receipt; do not re-check it against live `cat` results,
because a public read mirror may lag the writer by a few minutes (record visibility as a
warning). The read-side gates of §3 still apply to answers: a curated but unaudited concept
is `draft` or `verification_current: false`.

A deterministic watchdog ([docs/maintenance-watchdog.md](docs/maintenance-watchdog.md)) pages
humans on stale cursors, stuck or needs_human items and failed writer jobs. Agents add no
monitoring, polling or retries of their own.

### Legacy flow: rollback only

`ai-wiki maintain`, `ai-wiki audit`, `ai-wiki jobs --pending-audit` and
`skills/ai-wiki-maintainer` drive the old path: ingest, server-side Codex curation, server-side
Codex audit, and a ledger checkpoint. They work only while the writer still runs Codex
(`AIWIKI_LLM=codex` with `AIWIKI_INTAKE=curate` and `AIWIKI_AUDIT=codex`) and exist solely to
roll back (docs/final-cutover-runbook.md). A final-state writer answers their audit request
with 409. The ledger's semantics (exit codes, cooldowns, `needs_repair`, `--drop`) are in the
README's "Maintenance recovery".

## 5. Skill source of truth

Repository directories `skills/ai-wiki`, `skills/ai-wiki-curating-maintainer`,
`skills/ai-wiki-auditor`, `skills/okf-knowledge-curator` and the rollback-only
`skills/ai-wiki-maintainer` are canonical. Detect runtime drift with:

```bash
python3 scripts/sync_skills.py --check
```

Use `--apply` only when deliberately deploying those exact versions. It preserves
platform-managed `multica-metadata.json` and removes other stale skill files.
