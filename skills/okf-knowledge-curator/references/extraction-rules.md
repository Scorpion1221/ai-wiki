# Extraction rules

## Language

Write `title`, `description`, and body prose in the source's primary language unless the
user requests another. Preserve identifiers, table/column names, event names, enum values,
prices, dates, SQL, code, URLs, and brand names verbatim. Keep filenames stable lowercase
ASCII kebab-case and schema fields in their controlled vocabulary.

## Pass 1: evidence inventory

List headings, tables, code, dates, prices, formulas, event names, decisions, state claims,
and unresolved items. Mark high-risk claims: commercial metrics, experiment outcomes,
release/live state, prices, events, and data contracts.

For each fact record what the source can and cannot prove:

- requirement/discussion → intent or proposal
- mainline commit → merged code only
- production QA → availability at the observed time
- mature metric/data result → measured effect for its cohort/window

## Pass 2: durable candidates

Create by reuse unit, not heading/issue/task count. Prefer Product, Feature, Monetization,
Metric, Experiment, Data Source, Playbook, Decision, Risk, Reference, and OpenQuestion.
Create a standalone concept when it answers a future question by itself, has its own
lifecycle/validation path, is linked repeatedly, or contains a durable formula/contract.
Avoid tiny process fragments and verbatim issue mirrors.

## Pass 3: write strict v0.2

For each concept:

1. Copy only source-backed facts and preserve the evidence boundary.
2. Add strict frontmatter: status, generated, and structured sources.
3. Give cited sources stable ids and join claims with Markdown footnotes.
4. Add links only to existing related concepts.
5. Use `stale_after` for dynamic facts; do not store staleness as status.
6. Keep new/uncertain material transiently `draft` and omit `verified`; the independent
   audit finalizes retained records without faking verification.

## Skip or keep raw

- Generic preamble with no reusable fact.
- Duplicate/process chatter superseded by a final decision.
- Unsupported interpretation or material outside bundle purpose.
- Purely cosmetic document structure.

## Lifecycle and extensions

- `draft`: transient curation state awaiting audit.
- `stable`: consumption-ready at the stated boundary.
- `deprecated`: retained history, not current.

If the bundle retains `confidence`, use it for claim certainty only; never substitute it
for `verified` trust or `stale_after` freshness.
