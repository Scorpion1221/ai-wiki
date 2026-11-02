# Strict OKF v0.2 examples

These examples show structure only. Match concept prose to the source language.

## Feature

```markdown
---
type: Feature
title: Chrome Extension for Canvas
description: The extension integrates solving and study workflows into Canvas.
tags: [plugin, chrome-extension, canvas]
aliases: [Canvas 插件, Canvas 浏览器扩展]
status: draft
generated: {by: process:ai-wiki-curator, at: 2026-08-13T08:00:00Z}
stale_after: 2026-11-10
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

It remains `draft`: a PRD proves intended scope, not release. `stale_after` is the PRD's date
(2026-08-12) + 90 days, the plan row of the Signals date rule.

## Machine-verified metric definition

```markdown
---
type: Metric
title: Revenue per download
description: Settled USD revenue divided by attributed downloads for the same window.
tags: [metric, revenue]
aliases: [每下载收入, RPD]
status: stable
generated: {by: process:ai-wiki-curator, at: 2026-08-13T08:00:00Z}
verified: {by: process:finance-contract-audit, at: 2026-08-13T08:05:00Z}
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

The verifier establishes machine-confirmed trust; a normal edit must not refresh it. The source
gives no date, so it declares no `stale_after`: it never reads fresh, and readers fail closed.
