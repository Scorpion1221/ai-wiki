---
name: ai-wiki-auditor
description: >-
  Run the AI Wiki auditor: preflight with `ai-wiki doctor --role auditor`, take the auditor lease
  and a workspace with `ai-wiki review begin`, adversarially review each concept the writer's
  audit backlog lists against the frozen evidence it cites, record verified, corrected or
  unverified with `review verdict`, send them with `review submit` and close with `review end`.
  Use for scheduled or manual audit runs on a writer with AIWIKI_AUDIT=external; never for
  curation, and never on a correction your own run wrote.
---

# ai-wiki-auditor

This copy is a pointer. The real instructions ship with the `ai-wiki` CLI on this machine and update with it, so
they are always the version that matches the CLI.

1. Run `ai-wiki update --check`. If it shows a `help` row (`available: true` or skills not `current`), run
   `ai-wiki update && hash -r` (never between `review begin` and `review end`). A failed check or update: say so in one line, never retry it
   or install by hand, and go on only if `ai-wiki skill` shows `ai-wiki-auditor` `current`.
2. Run `ai-wiki skill`, read the `skill_md` it lists for `ai-wiki-auditor`, and follow that file, not this one.
3. If `ai-wiki` is not installed or that file cannot be read, stop blocked:
   "ai-wiki is not installed on this runtime". Never copy skills by hand.
