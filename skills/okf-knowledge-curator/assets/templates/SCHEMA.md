---
type: Contract
---

# SCHEMA — <bundle name>

> Strict OKF v0.2 contract. Read this and `purpose.md` before every ingest/update.

## Layers and purpose

- `sources/`: immutable raw-source snapshots. Store raw Markdown/text as `*.md.source`
  so concept discovery cannot mistake evidence for a concept.
- concept directories: AI-maintained durable knowledge.
- `SCHEMA.md` + `purpose.md`: human-defined boundary and quality contract.
- `.okf/`: operational history/health state, not concepts.

## Taxonomy

| type | directory | use for |
|---|---|---|
| Product, Product Overview | products/ | product surfaces and portfolio |
| Feature | features/ | reusable capability or flow |
| Monetization, Monetization Strategy | monetization/ | plans, pricing, paywall |
| Metric | metrics/ | definition, formula, denominator, caveats |
| Attested Computation | computations/ | sanctioned high-risk executable contract |
| Data Source | analytics/ | table, API, event source, filters |
| Experiment | experiments/ | design, assignment, readout, decision |
| Playbook | playbooks/ | repeatable procedure |
| Decision | decisions/ | context, decision, rationale |
| Risk | risks/ | issue, failure mode, monitoring signal |
| Reference, OpenQuestion | references/ | glossary/evidence/unresolved conflict |

## Creation threshold

Create a concept when it appears in two or more sources or is central and independently
reusable in one. Aggregate by durable user/business/engineering concept, never one page per
issue or task. Split concepts over about 200 lines only when the pieces remain reusable.

## Strict frontmatter

Required: `type`, `title`, `description`, non-empty `tags`, explicit `status`, `generated`,
and non-empty structured `sources`.

- `status`: `draft | stable | deprecated`.
- `generated`: `{by, at}`; meaningful content change only.
- `sources`: list of mappings, each with `resource`; add `id` for footnote joins. Use
  bundle-root absolute `/sources/<file>` for root source snapshots because relative paths
  resolve from the concept document.
- `verified`: optional real verification events; editing never creates/refreshes them.
  Trust follows all verification history. When no event is at or after `generated.at`,
  `verification_current` is false and the current revision is unconfirmed.
- `stale_after`: optional absolute `YYYY-MM-DD`; stale is not a status.
- Extensions such as `confidence`, `contested`, `contradictions`, and `source_sha256` are allowed.

Forbidden v0.1 forms: `timestamp`, `last_verified_at`, string sources,
`reviewed`/`canonical`/`stale`, and top-level `# Citations`.

## Attribution and links

Join claims to `sources[].id` with Markdown footnotes (`[^source-id]`). Link related
concepts with normal bundle-relative or file-relative Markdown links. Record genuine
conflicts reciprocally with `contested` and `contradictions`.

## Update policy

- New concepts are `draft` and unverified.
- Newer evidence may supersede older facts; preserve history with `deprecated` when useful.
- Update `generated` on meaningful edits, but never imply verification.
- For payments/revenue/renewal/LTV, experiment winners, and live/released claims, require
  direct evidence, non-stale freshness, and a real audit verification before stable use.
- Close out: indexes → log → strict validate → lint → source-hash commit; Git is service-owned.
