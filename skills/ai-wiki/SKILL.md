---
name: ai-wiki
description: >-
  Consult the team's curated OKF knowledge bundle through `ai-wiki`. Use for metric
  definitions/SQL, product or feature facts, event and field names, pricing, experiments,
  past decisions, risks, playbooks, and any request to look something up in the wiki.
  Cite concept paths and never invent facts that the CLI did not return.
---

# ai-wiki — consult curated team knowledge

`ai-wiki` is a deterministic read window over an authored OKF bundle. It runs no LLM on
reads. Treat its returned concepts as evidence, not memory, and cite the paths you use.

## Start from live context

```sh
ai-wiki                    # active bundle, status counts, directories, next commands
ai-wiki health             # full type/status counts
ai-wiki bundle list        # bundles hosted by this server
```

If the binary is missing, install it once with
`uv tool install git+https://github.com/Scorpion1221/ai-wiki`. If it is unconfigured, ask
the owner for the endpoint and token, then run:

```sh
ai-wiki config set --endpoint <url> --token <token>
ai-wiki bundle use <name>                 # -b <name> overrides once per command
```

Most metadata and collection commands emit compact TOON. `cat` deliberately emits raw
Markdown so its content stays readable. Structured commands that support `--json` keep it
as an escape hatch.

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

Search results already include path, title, status, description/snippet context, and total
count. Do not blind-`cat` every hit. Use `--top-k <N>` only when the default truncates a
relevant result set. `grep` prints at most 100 hits by default and tells you when to rerun
with `--limit 0`.

For vague, indirect, or bilingual questions, try 2–3 meaningfully different queries
(original wording, Chinese/English switch, and a paraphrase). Do not fan out mechanically
when the user's exact term already has one obvious home.

### 3. Trace relationships in both directions

```sh
ai-wiki links <dir>/<concept>.md
```

`links` returns outbound references and inbound backlinks. Follow one relevant hop when it
could change the answer—especially experiment ↔ metric ↔ decision/risk. Stop when the next
hop adds no relevant evidence; this is retrieval, not graph tourism 🤡

### 4. Read the source concept

```sh
ai-wiki cat <dir>/<concept>.md
ai-wiki cat <path> --full       # only if the preview reports truncation
```

`cat` previews up to 8,000 characters. It prints the total size and exact `--full` command
only when truncated, so do not request `--full` pre-emptively.

## Evidence discipline

- Cite concept paths, for example `metrics/<x>.md` and `experiments/<y>.md`.
- Trust only returned content. Never invent definitions, prices, event names, dates, or outcomes.
- Respect `status`, `confidence`, `contested: true`, and correction notes. Report the corrected
  value; label draft/low-confidence material as uncertain.
- Separate what a concept explicitly states from your inference across concepts.
- Definitive empty arrays/counts mean the command succeeded and found nothing; do not rerun just
  to verify emptiness.

## Submit sources only when asked

```sh
ai-wiki ingest notes.md
ai-wiki ingest report.pdf chart.png
cat notes.md | ai-wiki ingest - --title "<title>"
ai-wiki jobs <job-id>
```

Ingest submits sources; it never edits concepts directly. Read-only deployments may return
`403`. Re-submitting identical content is a successful no-op and returns the existing job.
For completed jobs, `ai-wiki jobs <job-id>` includes validation status, the exact commit,
and changed files. Bundle deletion is non-interactive and requires explicit `--yes`.

Use `ai-wiki <command> --help` for complete flags and examples. `-v`, `-V`, and `--version`
return the bare CLI version.
