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

Each search hit carries a `description` and a best-matching body `snippet` — read those
before deciding whether to `cat` a result; don't blind-cat every hit in the list.

Tip: when you know the exact term, `grep`/`ls` beat `search`; reach for `search` for
fuzzy or cross-language recall when the location is unknown.

**Trace the link graph both directions:**

```
ai-wiki links <dir>/<concept>.md   # outbound (what it cites) + inbound (what cites it)
```

`# Related concepts` in a page's body only shows outbound links. Use `links` to also see
*backlinks* — other concepts that point at this one — which is how you find, e.g., every
experiment/risk/playbook touching a metric, not just what that metric happens to link out to.

**For vague, indirect, or cross-language questions — fan out, then close the loop:**

1. Issue 2-3 `search` calls with different phrasings of the question: the user's original
   wording, an English/Chinese switch, and a paraphrase avoiding the docs' likely exact
   terms. Take the union of hits — a single phrasing under-recalls on a bilingual KB.
2. For each strong hit, run `ai-wiki links <path>` and skim one hop out (both directions).
   If that hop surfaces nothing new and relevant, treat coverage as complete; if it does,
   follow it before answering.
3. Only settle for a single `search` call + its `# Related concepts` when the question
   uses the docs' own terminology and clearly has one home (i.e. an "easy" lookup).

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
| `ai-wiki search "<q>" [--top-k N] [--json]` | ranked lexical search (CJK-aware); hits include description + snippet |
| `ai-wiki grep <pattern> [dir] [--fixed]` | regex search; `--fixed` = literal |
| `ai-wiki links <path> [--json]` | outbound + inbound (backlink) graph for one concept |
| `ai-wiki log [--tail N]` | change ledger — what was added/corrected, when |
| `ai-wiki ingest <files…>` / `jobs <id>` | submit source(s) — any type (md/pdf/image/text) — for curation (if writes enabled) |

## Limits

- Whole-graph / centrality questions (e.g. "most-referenced concept") belong to an offline
  graph view, not this CLI — `links` covers one concept's neighborhood, not the full graph.
- Read-only deployments return `403` on `ingest` — that's expected.
