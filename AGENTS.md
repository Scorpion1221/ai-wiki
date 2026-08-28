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
ai-wiki log                           # change ledger
```

Orient through `SCHEMA.md`/`purpose.md`, then drill or search. Follow one relationship hop
when it can change the answer, especially experiment ↔ metric ↔ decision/risk.

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
ai-wiki audit <ingest-job-id>            # only after ingest status is done
ai-wiki jobs <audit-job-id>
```

Files are stored verbatim. Supported text/code/image sources are curated; PDF and other
opaque formats remain `needs-conversion` rather than being guessed. Identical submissions and
repeated audits are successful idempotent no-ops.

Ingest completion is not verification. Interpret the audit terminal result:

- `passed`: all concepts affected by that ingest were verified;
- `needs_attention`: review completed, but some concepts remain unverified; do not retry
  without new evidence. Completed concepts are stable but unverified, never long-lived draft;
- `passed` + `reason: no_concepts_to_audit`: ingest changed no concept files, so audit
  completed immediately without a reviewer or audit commit;
- job `failed`: technical, validation, or Git failure.

Advance an automation checkpoint only after audit job `done` (`passed` or
`needs_attention`), never after failure, timeout, or API error. The worker owns validation,
commit and push. A real Git conflict aborts and retries from fresh remote state rather
than running an LLM conflict resolver. A public read-only mirror may lag the writer, so
compare reported bundle Git revisions rather than assuming a push is already visible.

## 5. Skill source of truth

Repository directories `skills/ai-wiki`, `skills/ai-wiki-maintainer`, and
`skills/okf-knowledge-curator` are canonical. Detect runtime drift with:

```bash
python3 scripts/sync_skills.py --check
```

Use `--apply` only when deliberately deploying those exact versions. It preserves
platform-managed `multica-metadata.json` and removes other stale skill files.
