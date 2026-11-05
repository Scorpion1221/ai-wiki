---
name: ai-wiki-curating-maintainer
description: >-
  Run the AI Wiki maintainer that curates: preflight with `ai-wiki doctor --role curator`,
  deterministic collection with `ai-wiki maint begin`, then serially claim each work item, curate
  it in the pulled workspace with okf-knowledge-curator, `validate` it locally and `propose` it to
  the writer's deterministic gate, and close with the deterministic `maint end` report. Use for
  scheduled or manual curating-maintainer runs; not for the legacy `ai-wiki maintain` ledger flow.
---

# ai-wiki-curating-maintainer

This copy is a pointer. The real instructions ship with the `ai-wiki` CLI on this machine and update with it, so
they are always the version that matches the CLI.

1. Run `ai-wiki update --check`. If it shows a `help` row (`available: true` or skills not `current`), run
   `ai-wiki update && hash -r` (never between `maint begin` and `maint end`). A failed check or update: say so in one line, never retry it
   or install by hand, and go on only if `ai-wiki skill` shows `ai-wiki-curating-maintainer` `current`.
2. Run `ai-wiki skill`, read the `skill_md` it lists for `ai-wiki-curating-maintainer`, and follow that file, not this one.
3. If `ai-wiki` is not installed or that file cannot be read, stop blocked:
   "ai-wiki is not installed on this runtime". Never copy skills by hand.
