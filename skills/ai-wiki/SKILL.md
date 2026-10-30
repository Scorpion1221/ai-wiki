---
name: ai-wiki
description: >-
  Consult the team's curated OKF v0.2 knowledge bundle through `ai-wiki`. Use for metric
  definitions/SQL, product or feature facts, event and field names, pricing, experiments,
  past decisions, risks, playbooks, and any request to look something up in the wiki.
  Cite concept paths, enforce status/trust/freshness gates, and never invent facts that
  the CLI did not return.
---
<!-- ai-wiki:version 0.3.7 -->

# ai-wiki — consult curated team knowledge

`ai-wiki` is a deterministic read window over an authored OKF v0.2 bundle. It runs no LLM
on reads. Treat returned concepts as evidence, not memory, and cite the paths you use.

## Start from live context

```sh
ai-wiki                    # active bundle, OKF version, trust/freshness counts, directories
ai-wiki health             # status/trust/freshness counts + bundle Git revision
ai-wiki bundle list        # bundles hosted by this server
```

If the binary is missing, install it once with
`uv tool install --force git+https://github.com/Scorpion1221/ai-wiki && hash -r && ai-wiki skill install`.
Before the first lookup of a task, run `ai-wiki update --check` (one read of `main`). A `help` row
(`available: true` or `skills` not `current`) → run `ai-wiki update && hash -r`. Then, updated or
not, compare this file's `<!-- ai-wiki:version X -->` with `ai-wiki --version`: if they differ, read
the installed copy at the path `ai-wiki skill` lists; if you cannot, stop blocked:
"workspace Skill is behind the CLI; publish it". A failed check (offline) → say so in one line, go
on with the installed version. A failed `ai-wiki update` → report it in one line, never retry it or
install by hand; go on only if `ai-wiki skill` shows this file `current`, else stop blocked. Never
update between `maint begin` or `review begin` and its `end` (the run holds a lease).
Never copy skills by hand; an ai-wiki without `update` needs the install command above.
The token comes from `AIWIKI_TOKEN`, which wins over a saved token: never put a token in a
command, chat or file. No token (`ai-wiki config show` has `token_source: null`) → stop and ask
the user to set `AIWIKI_TOKEN`. No endpoint → ask the owner for the URL, then run:

```sh
ai-wiki config set --endpoint <url>
ai-wiki bundle use <name>                 # -b <name> overrides once per command
```

This client and its bundles target **OKF v0.2 only**. Do not write or infer legacy v0.1
fields such as `timestamp`, string-valued `sources`, `last_verified_at`, or statuses
`reviewed`/`canonical`/`stale`.

## Lookup workflow

### 1. Orient once per bundle

```sh
ai-wiki cat SCHEMA.md       # taxonomy, conventions, statuses, update policy
ai-wiki cat purpose.md      # scope and purpose
ai-wiki ls                  # directory map
```

### 2. Choose the cheapest retrieval primitive

```sh
ai-wiki ls <dir>                         # known directory
ai-wiki grep "<exact term>" --fixed      # exact event, field, table, path, price, symbol
ai-wiki grep "<regex>"                   # regex across concepts
ai-wiki search "<fuzzy keywords>"        # ranked, CJK-aware discovery
```

TOON list output separates semantic `concepts` from structural `entries`; only concepts carry
`status`, `trust`, `freshness`, `generated_at`, `verified_at`, `verification_current`, `stale_on`
and `basis` (`freshness_basis` in JSON). `ls --json` returns the complete raw entry array. Search
covers every concept at any depth and ranks exact phrase and full query coverage ahead of repeated
partial tokens (`phrase`, `coverage`, `fields` and `terms` explain each match); trust/freshness only
break close ties, so apply the evidence gate after retrieval.

Run one well-formed search. Read the top candidates when `phrase: true` or `coverage: 1.0` makes
the match clear; when coverage is partial or scattered, rewrite it once (the user's other wording,
a paraphrase, the other language). Zero hits is not "nothing": retry with synonyms or the table,
event or field name (`grep --fixed`) before saying the wiki has nothing. Do not fan out further.
JSON `search`/`grep`/`log` output carries `shown`, `total` and `truncated`: check them before
treating bounded output as complete. Never rerun an identical command to confirm an empty answer.

### 3. Read the concept, then trace one hop

```sh
ai-wiki cat <dir>/<concept>.md          # raw Markdown preview
ai-wiki cat <dir>/<concept>.md --json   # content + derived OKF metadata for the gates
ai-wiki cat <dir>/<concept>.md --full   # when the preview ends with a truncation notice
ai-wiki links <dir>/<concept>.md        # outbound + inbound relationships
```

Never pipe `cat` through `head`, `grep` or `tail`: that hides the closing
`<!-- ai-wiki: truncated … -->` notice; on that notice, rerun with `--full`. For identity, payment,
experiment and attribution questions, and whenever experiment ↔ metric ↔ decision/risk could
change the answer, run `links` and read one relevant hop. Stop when the next hop adds nothing.

## Evidence gate: status + trust + freshness

Derive meaning only from OKF v0.2 signals:

- `status`: `draft | stable | deprecated` (a missing status is not acceptable in the team profile).
- `trust`: `unverified`, `machine-confirmed` or `human-reviewed`, derived from all `verified`
  events (OKF v0.2 §5.3), so it can reflect a review of an older revision.
- `verification_current`: a `verified` event confirms the current revision: one at or after
  `generated.at`, or, for an event by the concept's own `process:` generator, at least 12 h after
  it (no process confirms its own fresh output). `false` → treat the revision as unverified for
  current facts, whatever its trust tier. `current_verified_at` names the confirming event.
- `freshness`: `fresh` only before a declared `stale_after`; `stale` on and after its stale date
  (`stale_on`): the declared one, or 90 days after `generated.at` when none is declared
  (`basis: default`); else `unspecified`. The default never makes a concept fresh.

Use this order for **current facts**:

1. `stable + fresh + human-reviewed`
2. `stable + fresh + machine-confirmed`
3. `stable + fresh + unverified` — usable only with an explicit verification caveat
4. `draft` — transient curation state before audit; process/context only
5. `stale` (basis `default` too) or `deprecated` — historical context only, not a current answer

`freshness: unspecified` is not proof of freshness: state `generated_at` and the latest
verification, and narrow the claim. A dated reading (a metric for a stated window, a status on a
date) stays a reading of that window: give it with its window, never as today's value.

### Fail closed for high-risk claims

For **commercial metrics** (payment, revenue, renewal, LTV), **experiment winners**, and
**“the feature is released/live”** claims, require all of:

- `status: stable`;
- `freshness: fresh` (`today < stale_after`, a declared date; a default basis is never fresh);
- `verification_current: true` (above);
- sources that directly prove the claimed boundary (requirement ≠ merged code ≠ release ≠ production effect).
  A high-risk claim known only from an attributed report (below) is never current truth.

If any condition fails, do not output the claim as current truth. Say what is missing or
expired and, if useful, report only the narrower source-backed statement.

## Answer discipline

- In the visible reply, cite every concept path you used with its status, trust and verification
  date, e.g. `metrics/x.md` (stable, machine-confirmed, verified 2026-10-12; say "not current" when
  `verification_current` is false). Cite only concepts you read with `cat`, never one seen only in
  search or grep results.
- Trust only returned content. Never invent definitions, prices, events, dates, or outcomes.
- A claim the concept attributes to an issue ("WAIO-722 reported…", "据 WAIO-722 报告…",
  "Reportedly,", "据报，") is that report, whatever the concept's trust: relay it as a report
  with its issue and the date it states (never `generated_at`/`verified_at`), and never call
  its content verified or machine-confirmed. The audit confirmed the report, not its truth.
- Surface `contested` and `contradictions`: name both concepts and their claims, never choose
  silently. Separate explicit facts from your inference across concepts.

## Submit sources only when asked

```sh
ai-wiki ingest notes.md
ai-wiki ingest report.pdf chart.png
cat notes.md | ai-wiki ingest - --title "<title>"
ai-wiki jobs <job-id>
```

Ingest submits sources; it never edits concepts. The server runs no LLM: a submission is
committed to the wiki's Git at once (text redacted of secrets; an image as sent, so never
submit one that shows a secret) and becomes a work item that the maintainer agent
curates on its next run (once a day at 02:00 CST, so within about a day). The answer and
`ai-wiki jobs <id>` show that intake commit, or say the writer is still retrying it; the job
then follows the item until a changeset curates it (with the commit) or the maintainer skips
it (with the reason); do not poll it in a loop. Re-submitting identical content is a
successful no-op. A terminal `needs-conversion` result means the format was stored but cannot
be curated, and nothing of it is committed: convert it to a directly readable file (UTF-8
text, or an image) and submit that as a new source.
Read-only deployments may return `403`.

Curation is not verification. A separate auditor agent verifies curated concepts on its own
schedule; until it has, a new concept is `draft` and a changed one has
`verification_current: false`, and the evidence gate above treats both accordingly. Never
claim a submission is verified because it was curated.

Maintaining the bundle is a role with its own credential and skill
(`ai-wiki-curating-maintainer`; `ai-wiki-auditor` for audits), not something this skill
does. `ai-wiki maintain` and `ai-wiki audit` are legacy CLI commands of the retired Codex flow.

Use `ai-wiki <command> --help` for complete flags and examples. `-v`, `-V`, and `--version`
return the bare CLI version.
