# Strict OKF v0.2 examples

These examples show structure only. Match concept prose to the source language.

## Feature

```markdown
---
type: Feature
title: Chrome Extension for Canvas
description: The extension integrates solving and study workflows into Canvas.
tags: [plugin, chrome-extension, canvas]
status: draft
generated: {by: process:ai-wiki-curator, at: 2026-08-13T08:00:00Z}
stale_after: 2026-09-12
sources:
  - id: canvas-prd
    resource: /sources/canvas-extension-prd.md.source
    title: Canvas extension PRD
    author: team:extension
    last_modified: 2026-08-12
---

# Capabilities

The PRD proposes screenshot solving and Canvas task integration.[^canvas-prd]

[^canvas-prd]: Canvas extension PRD, capabilities section.
```

It remains `draft`: a PRD proves intended scope, not release.

## Machine-verified metric definition

```markdown
---
type: Metric
title: Revenue per download
description: Settled USD revenue divided by attributed downloads for the same window.
tags: [metric, revenue]
status: stable
generated: {by: process:ai-wiki-curator, at: 2026-08-13T08:00:00Z}
verified: {by: process:finance-contract-audit, at: 2026-08-13T08:05:00Z}
stale_after: 2026-09-12
sources:
  - id: finance-contract
    resource: analytics/web-commercial-data-contract.md
    title: Web commercial data contract
---

# Formula

`settled_usd_revenue / attributed_downloads`.[^finance-contract]

# Caveats

The numerator and denominator must use the same attribution window.[^finance-contract]

[^finance-contract]: Web commercial data contract, revenue and attribution sections.
```

The verifier establishes machine-confirmed trust; a normal edit must not refresh it.
