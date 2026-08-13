# Concept Taxonomy

Choose the narrowest durable type that matches how the knowledge will be reused.

## Common types

- `Product Overview`: product matrix, portfolio, top-level positioning.
- `Product`: a product surface or app, e.g. Web main site, Chrome Extension.
- `Feature`: reusable product capability, flow, or UX/business mechanism.
- `Monetization`: plan, entitlement, pricing package, paywall rule.
- `Monetization Strategy`: broader pricing or conversion strategy.
- `Metric`: definition, formula, denominator, caveats.
- `Attested Computation`: sanctioned high-risk computation with runtime, parameters,
  executor receipt, and deterministic attester contract.
- `Experiment`: AB test design, groups, assignment signals, readout, decision signal.
- `Data Source`: table, API, event source, schema notes, filters.
- `Playbook`: repeatable procedure or SQL analysis template.
- `Decision`: decision record with context, decision, rationale, implications.
- `Risk`: known issue, alert, failure mode, monitoring signal.
- `Reference`: glossary, competitor note, enum list, external concept.
- `Meeting`: meeting notes only when the meeting itself is the reusable artifact.
- `WeeklyInsight`: high-value weekly-report insight with owner and source.
- `OpenQuestion`: unresolved question requiring follow-up.

## Splitting heuristics

Split into a separate concept when:

- It can answer a future question by itself.
- It has its own owner, metric, lifecycle, or validation path.
- It is linked by multiple other concepts.
- It contains SQL/formula/event names that should not be hidden in prose.

Keep together when:

- The section is only explanatory context for one concept.
- It has no independent reuse or validation path.
- Splitting would create tiny fragments with no future retrieval value.
