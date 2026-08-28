---
name: ai-wiki
description: >-
  Consult the team's curated OKF v0.2 knowledge bundle through `ai-wiki`. Use for metric
  definitions/SQL, product or feature facts, event and field names, pricing, experiments,
  past decisions, risks, playbooks, and any request to look something up in the wiki.
  Cite concept paths, enforce status/trust/freshness gates, and never invent facts that
  the CLI did not return.
---

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
`uv tool install git+https://github.com/Scorpion1221/ai-wiki`. If it is unconfigured, ask
the owner for the endpoint and token, then run:

```sh
ai-wiki config set --endpoint <url> --token <token>
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
ai-wiki grep "<exact term>" --fixed      # exact event, field, path, price, symbol
ai-wiki grep "<regex>"                   # regex across concepts
ai-wiki search "<fuzzy keywords>"        # ranked, CJK-aware discovery
```

Search and list results expose `status`, `trust`, `freshness`, `generated_at`, `verified_at`,
and `verification_current`. Search also explains its lexical match with `phrase`, `coverage`,
`fields`, and `terms`. It normalizes punctuation/separators, preserves Latin word boundaries
and CJK bigrams, searches every concept regardless of directory depth, and ranks exact phrase
plus full query coverage ahead of repeated partial tokens. Trust/freshness only break close
textual ties; apply the evidence gate after retrieval rather than silently hiding weak or
historical evidence.

Run one well-formed search first. Read the top candidates when `phrase: true` or
`coverage: 1.0` makes the match clear. Rewrite the query at most once when results are empty,
coverage is partial, or matches are scattered across weak fields; use the user's alternative
wording, a concise paraphrase, or the other language. Do not mechanically fan out several
queries. Exact identifiers still belong to `grep --fixed`.

### 3. Trace relationships, then read the concept

```sh
ai-wiki links <dir>/<concept>.md
ai-wiki cat <dir>/<concept>.md
ai-wiki cat <dir>/<concept>.md --json  # content + derived OKF metadata
ai-wiki cat <path> --full       # only if the preview reports truncation
```

Follow one relevant graph hop when it could change the answer—especially experiment ↔
metric ↔ decision/risk. Stop when the next hop adds no relevant evidence.
Default `cat` stays readable raw Markdown. Use `--json` when an agent needs the response's
`metadata` object to apply status/trust/freshness gates mechanically alongside `content`.

## Evidence gate: status + trust + freshness

Derive meaning only from OKF v0.2 signals:

- `status`: `draft | stable | deprecated` (missing status is not acceptable in the team profile).
- `trust`: `unverified`, `machine-confirmed`, or `human-reviewed`, derived from all
  `verified` events exactly as OKF v0.2 §5.3 specifies.
- `freshness`: `fresh`, `stale`, or `unspecified`, derived from `stale_after`.

A trust tier can reflect historical review. `verification_current: false` means no
verification event is at or after `generated_at`; treat the current revision like
unverified for current-fact decisions even if its standards-compliant displayed tier is
machine-confirmed/human-reviewed. `current_verified_at` identifies the latest event that
does confirm the revision, when present.

Use this order for **current facts**:

1. `stable + fresh + human-reviewed`
2. `stable + fresh + machine-confirmed`
3. `stable + fresh + unverified` — usable only with an explicit verification caveat
4. `draft` — transient curation state before audit; process/context only
5. `stale` or `deprecated` — historical context only, not a current answer

`freshness: unspecified` is not proof of freshness. State the latest `generated_at` and
`verified_at` available and narrow the claim. If two usable concepts disagree, report the
conflict and follow `contested`/`contradictions` rather than choosing silently.

### Fail closed for high-risk claims

For **commercial metrics** (payment, revenue, renewal, LTV), **experiment winners**, and
**“the feature is released/live”** claims, require all of:

- `status: stable`;
- `freshness: fresh` (`today < stale_after`);
- `verification_current: true` (a `verified` event exists at or after `generated.at`);
- sources that directly prove the claimed boundary (requirement ≠ merged code ≠ release ≠ production effect).

If any condition fails, do not output the claim as current truth. Say what is missing or
expired and, if useful, report only the narrower source-backed statement.

## Answer discipline

- Cite concept paths, for example `metrics/<x>.md` and `experiments/<y>.md`.
- Trust only returned content. Never invent definitions, prices, events, dates, or outcomes.
- Surface `status`, `trust`, `freshness`, and verification date when they affect the answer.
- Separate explicit facts from your inference across concepts.
- Definitive empty arrays/counts mean the command succeeded and found nothing; do not rerun
  just to verify emptiness.

## Submit sources only when asked

```sh
ai-wiki ingest notes.md
ai-wiki ingest report.pdf chart.png
cat notes.md | ai-wiki ingest - --title "<title>"
ai-wiki jobs <ingest-job-id>
```

Ingest submits sources; it never edits concepts directly. Read-only deployments may return
`403`. Re-submitting identical content is a successful no-op and returns the existing job.
A completed ingest reports validation, commit, and changed files. Maintainers must then run
the separate audit workflow from the `ai-wiki-maintainer` skill; ordinary readers should
not claim an ingest is verified merely because curation completed. A terminal
`needs-conversion` result means the format was stored but not curated; convert it to a
directly readable artifact and ingest that as a new source rather than polling forever.

Use `ai-wiki <command> --help` for complete flags and examples. `-v`, `-V`, and `--version`
return the bare CLI version.
