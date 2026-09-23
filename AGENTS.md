# Using ai-wiki (agent guide)

`ai-wiki` is a deterministic read window onto a curated **OKF v0.2 knowledge bundle**
served over HTTP. Reads run no LLM; curation and adversarial audit are separate write-side
jobs. This guide is how an agent installs the CLI, connects, reads safely, and submits
source evidence.

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

## 4. Submit, then audit (when writes are enabled)

Agents never edit concepts or the bundle Git repository directly. Submit sources:

```bash
ai-wiki ingest notes.md
ai-wiki ingest a.md config.json chart.png
cat notes.md | ai-wiki ingest - --title "<stable source identity>"
ai-wiki jobs <ingest-job-id>
ai-wiki jobs --pending-audit --json  # ingests older than 24h missing an audit
ai-wiki audit <ingest-job-id>            # only after ingest status is done
ai-wiki jobs <audit-job-id>
ai-wiki -b my-kb maintain --manifest sources.json --state-dir ~/.ai-wiki/maintenance/my-kb --audit-pending
```

`maintain` owns durable source/job state, polling and safe retries. Reuse the same state
directory, never edit its `state.json`, and preserve all attempt receipts. Every entry ends a
run as `done`, `pending` (a later run resumes it after its cooldown), `needs_repair` (attempt
cap reached, or a non-retryable failure; it never blocks other sources), or `superseded` (a
newer version of the same identity replaced an unfinished one). Exit codes: `0` everything is
done or superseded; `1` work is pending, the runner lock is held, or a read failed (see
`warnings`), which is normal; `3` some entries need repair while other work ran; `2` usage or
state error. Retries follow the failed job's `failure.class`: capacity cools down for an hour
and stops the batch; transient, timeout, interrupted, conflict and model_output get 3 attempts
and internal 2; auth, disk and input need repair. Omit `--manifest` to resume only;
`--retry-now` skips cooldowns once, never caps or rollback gates; `--status` reads the saved
state offline. The CLI adds no scheduler.

Files are stored verbatim. Supported text/code/image sources are curated; PDF and other
opaque formats remain `needs-conversion` rather than being guessed. Identical submissions and
repeated audits are successful idempotent no-ops.

Ingest completion is not verification. Interpret the audit terminal result:

- `passed`: all concepts affected by that ingest were verified;
- `needs_attention`: review completed, but some concepts remain unverified; do not retry
  without new evidence. Completed concepts are stable but unverified, never long-lived draft.
  Exception: `audit.reason: verdict_missing` or `verdict_invalid` means the reviewer gave no
  usable verdict (a format slip, not an evidence judgment). One more `ai-wiki audit
  <ingest-job-id>` is allowed and `maintain` does it automatically; after a second slip the
  writer returns the latest attempt as a deduplicated result;
- `passed` + `reason: no_concepts_to_audit`: ingest changed no concept files, so audit
  completed immediately without a reviewer or audit commit;
- job `failed`: technical, validation, or Git failure. `failure {class, retryable,
  retry_after_s, stage, detail}` says why.

A collection cursor is decoupled from completion: advance it once every selected source is
frozen into the `maintain` ledger (exit `0`, `1` or `3` with each manifest source in the
summary; exit `2` or an `{"error"}` result froze nothing), not when its audit ends.
Pending and needs-repair sources stay in the ledger and are retried later; they never hold the
cursor back. A source is complete only when its audit job is `done` (`passed` or
`needs_attention`), never after failure, timeout, or API error. The worker owns validation,
commit and push. A real Git conflict aborts and retries from fresh remote state rather
than running an LLM conflict resolver. A public read-only mirror may lag the writer, so
record mirror visibility separately rather than assuming a push is already visible.
Curator verification-history edits are deterministically discarded/restored by the worker; only
an audit can confirm a new generation. Repairs are recorded in the Job receipt.
The durable audit Job (done + passed validation + passed/needs_attention + successful Git
result when applicable) is the per-source completion receipt. Do not re-check that receipt
against live `cat` results: mirror lag, a missing new page, or a subsequent ingest must not
turn a completed audit into a failed maintenance run. Mirror visibility is a warning, not a
checkpoint gate; query-side evidence gates still apply to answers from returned content.

A deterministic watchdog ([docs/maintenance-watchdog.md](docs/maintenance-watchdog.md))
pages on a stale checkpoint, stuck runs, `needs_repair` or long-pending ledger entries
(`ledger_needs_repair`, `ledger_pending_stale`), and writer job failures with no later
attempt (`job_failed`, for up to 7 days, so retry or deliberately drop a failed source within
the week). Agents must not add their own monitoring.

## 5. Skill source of truth

Repository directories `skills/ai-wiki`, `skills/ai-wiki-maintainer`, and
`skills/okf-knowledge-curator` are canonical. Detect runtime drift with:

```bash
python3 scripts/sync_skills.py --check
```

Use `--apply` only when deliberately deploying those exact versions. It preserves
platform-managed `multica-metadata.json` and removes other stale skill files.
