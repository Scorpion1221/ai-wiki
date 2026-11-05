---
name: okf-knowledge-curator
description: >-
  Curate messy source documents into strict Open Knowledge Format v0.2 bundles. Use when
  the AI Wiki worker or the curating maintainer (in a pulled workspace) needs to split
  sources into durable concepts, preserve structured provenance and per-claim attribution,
  leave generated/verified bookkeeping to the service without faking verification, and hand
  concept edits back to deterministic service gates.
---

# okf-knowledge-curator

This copy is a pointer. The real instructions ship with the `ai-wiki` CLI on this machine and update with it, so
they are always the version that matches the CLI.

1. Run `ai-wiki update --check`. If it shows a `help` row (`available: true` or skills not `current`), run
   `ai-wiki update && hash -r` (never during a `maint` or `review` run). A failed check or update: say so in one line, never retry it
   or install by hand, and go on only if `ai-wiki skill` shows `okf-knowledge-curator` `current`.
2. Run `ai-wiki skill`, read the `skill_md` it lists for `okf-knowledge-curator`, and follow that file, not this one.
3. If `ai-wiki` is not installed or that file cannot be read, stop blocked:
   "ai-wiki is not installed on this runtime". Never copy skills by hand.
