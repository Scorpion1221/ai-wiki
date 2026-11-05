---
name: ai-wiki
description: >-
  Consult the team's curated OKF v0.2 knowledge bundle through `ai-wiki`. Use for metric
  definitions/SQL, product or feature facts, event and field names, pricing, experiments,
  past decisions, risks, playbooks, and any request to look something up in the wiki.
  Cite concept paths, enforce status/trust/freshness gates, and never invent facts that
  the CLI did not return.
---

# ai-wiki

This copy is a pointer. The real instructions ship with the `ai-wiki` CLI on this machine and update with it, so
they are always the version that matches the CLI.

1. Run `ai-wiki update --check`. If it shows a `help` row (`available: true` or skills not `current`), run
   `ai-wiki update && hash -r` (never between `maint begin` or `review begin` and its `end`). A failed check or update: say so in one line, never retry it
   or install by hand, and go on only if `ai-wiki skill` shows `ai-wiki` `current`.
2. Run `ai-wiki skill`, read the `skill_md` it lists for `ai-wiki`, and follow that file, not this one.
3. If `ai-wiki` is not installed or that file cannot be read, stop blocked:
   "ai-wiki is not installed on this runtime". Never copy skills by hand.
