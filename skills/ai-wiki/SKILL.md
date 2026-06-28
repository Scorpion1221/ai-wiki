---
name: ai-wiki
description: >-
  Consult a curated OKF knowledge bundle — products, features, metrics & their
  definitions/SQL, experiments and their readouts, data sources, playbooks, decisions,
  and risks — through the read-only `ai-wiki` CLI. Use whenever a question's answer
  likely lives in the team's knowledge base / wiki: a metric's definition or how it is
  computed, an experiment result, a past decision, an event/field name, a price, or
  "look it up in the wiki". Navigate with ls/cat/search/grep, trust only what the CLI
  returns, and cite the concept paths used. Read-only by default (writes go through ingest).
---

# ai-wiki — consult the knowledge base

`ai-wiki` is a read-only window onto a curated OKF knowledge bundle served over an HTTP
API. The service runs no LLM — everything it returns is authored, verifiable content.
Trust it over memory, and cite the concept paths you use.

## 0. Be configured + reachable

```
ai-wiki health          # active bundle: size + counts by type/status
ai-wiki bundle list     # knowledge bases hosted on the server (* = active)
```

One server hosts many bundles. If unconfigured, connect (ask the owner for endpoint + token),
then pick a bundle:

```
ai-wiki config set --endpoint <url> --token <token>   # connect to the server
ai-wiki bundle use <name>     # switch active bundle;  -b <name> overrides per command
ai-wiki bundle create <name>  # create a new empty bundle on the server (if writes allowed)
```

## 1. Orient first (once per session)

```
ai-wiki cat SCHEMA.md      # concept taxonomy, conventions, update policy
ai-wiki cat purpose.md     # why this KB exists, scope
ai-wiki ls                 # the directory map (each dir = a count + description)
```

## 2. Find content — two ways, usually combined

**Drill (filesystem-like, the default):**

```
ai-wiki ls                       # top-level dirs + descriptions
ai-wiki ls <dir>                 # concepts in a directory
ai-wiki cat <dir>/<concept>.md   # read one
```

Every concept ends with a `# Related concepts` section of links — **follow them** to
pull the thread (e.g. experiment → metric → risk → decision). That is how relationships
are traced.

**Search (for fuzzy recall, or when you don't know where it lives):**

```
ai-wiki search "<keywords>" --top-k 8   # ranked, CJK-aware
ai-wiki grep "<pattern>"                # regex across all concepts
ai-wiki grep "<literal>" --fixed        # literal — use --fixed for paths/symbols
```

Tip: when you know the exact term, `grep`/`ls` beat `search`; reach for `search` for
fuzzy or cross-language recall when the location is unknown.

## 3. Answer with discipline

- **Cite concept paths**: e.g. "Based on `metrics/<x>.md` and `experiments/<y>.md`…".
- **Only trust CLI output.** Never invent metric definitions, prices, event names, dates,
  or experiment outcomes the CLI did not return.
- **Respect status/confidence.** Concepts carry `status` (draft/reviewed/canonical/stale)
  and `confidence`. Flag uncertainty for `draft`/low-confidence concepts rather than
  presenting them as settled. Watch for `contested: true` or a `⚠️ …correction` note — a
  prior conclusion was corrected; report the corrected value, not the old one.

## Command reference

| Command | Use |
|---|---|
| `ai-wiki health` | bundle overview (counts by type/status) |
| `ai-wiki config set --endpoint <url> --token <tok>` | connect to the server hosting the bundles |
| `ai-wiki bundle list/use/create/rm` | bundles on the server: list, switch active, create, delete (`rm` confirms; `-y` to skip) |
| `ai-wiki -b <name> <cmd>` | run one command against a non-active bundle |
| `ai-wiki ls [dir] [-R] [-a] [--json]` | list a level like `ls`; `-R` recurse, `-a` dotfiles |
| `ai-wiki cat <path>` | read a concept (or any file in the bundle) |
| `ai-wiki search "<q>" [--top-k N] [--json]` | ranked lexical search (CJK-aware) |
| `ai-wiki grep <pattern> [dir] [--fixed]` | regex search; `--fixed` = literal |
| `ai-wiki log [--tail N]` | change ledger — what was added/corrected, when |
| `ai-wiki ingest <file>` / `jobs <id>` | submit a source for curation (if writes are enabled) |

## Limits

- Links are **forward-only** (a concept lists what it references). There is no `backlinks`
  command — to find "what links here", `grep` the filename. Whole-graph / centrality
  questions belong to an offline graph view, not this CLI.
- Read-only deployments return `403` on `ingest` — that's expected.
