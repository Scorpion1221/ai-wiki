# Strict OKF v0.2 profile

## Bundle conformance

- Root `index.md` declares `okf_version: "0.2"`.
- Directory indexes have no frontmatter and list concepts for progressive disclosure.
- `log.md` date headings use `## YYYY-MM-DD`.
- `SCHEMA.md` and `purpose.md` are structural documents with exactly `type: Contract`
  frontmatter; they are exempt from the full knowledge-concept profile below.
- Raw Markdown/text evidence is stored as `sources/*.md.source`, not `*.md`, so it is
  outside concept discovery while remaining directly readable by the curator.
- Every non-reserved Markdown concept has parseable frontmatter and a non-empty `type`.

The AI Wiki profile deliberately adds stricter authoring requirements than the official
minimum. Unknown types and extension keys remain allowed.

## Required knowledge-concept fields

```yaml
type: <Concept type>
title: <Human-readable title>
description: <One sentence>
tags: [non-empty, string-list]
status: draft | stable | deprecated
generated:
  by: <human:id | process:id | producer/version>
  at: <ISO 8601 datetime>
sources:
  - id: <stable claim key>       # required when body cites it
    resource: <URL, /bundle-root path, concept-relative path, or scope descriptor>
    title: <optional label>
    author: <optional actor>
    usage_count: <optional integer>
    last_modified: <optional YYYY-MM-DD>
```

`sources` is a non-empty list of mappings; every item requires a non-empty `resource`.
A bundle-root source snapshot should be `/sources/<file>`; bare `sources/<file>` resolves
under a subdirectory concept and is usually wrong. A correct relative alternative is for
example `../sources/<file>` from a one-level concept directory.
A shared `usage_window: {from, to}` may frame source `usage_count`, or an entry may override
it. These are credibility signals, not a stored credibility score.

## Optional trust and freshness

```yaml
verified:
  - {by: process:nightly-review, at: 2026-08-13T02:00:00Z}
  - {by: human:owner-id, at: 2026-08-13T09:00:00Z}
stale_after: 2026-09-12
```

A bare `verified: {by, at}` mapping is valid and means a one-element list. Trust is derived:

- absent/empty verified → unverified
- only non-human actors → machine-confirmed
- any `human:` actor → human-reviewed

Freshness is derived by comparing the current date with `stale_after`; missing
`stale_after` means unspecified, not proven fresh. `generated` and `verified` are
independent: editing updates `generated`, never verification. Historical verification may
be retained. The OKF trust tier still derives from all `verified` history; separately, when
no `verified.at >= generated.at`, `verification_current` is false, so the current revision
is not confirmed and must be re-audited.

## Per-claim source join

```markdown
The fact being attributed.[^source-id]

[^source-id]: Human-readable locator within the source.
```

The footnote label must match `sources[].id`. The mapping, not the footnote prose, is the
machine-readable source of truth.

## Allowed extensions

Bundle-specific fields such as `confidence`, `source_type`, `source_ref`, `source_sha256`,
`owner`, `contested`, and `contradictions` may remain. They must not replace standard OKF
v0.2 trust, freshness, or provenance fields.

## Rejected v0.1 forms

Strict validation rejects:

- `timestamp`
- `last_verified_at`
- string entries in `sources`
- statuses `reviewed`, `canonical`, or `stale`
- a top-level `# Citations` section

There is no legacy fallback in the CLI or writer.
