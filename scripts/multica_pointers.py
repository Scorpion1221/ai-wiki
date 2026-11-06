#!/usr/bin/env python3
"""Generate (or ``--check``) the Multica workspace copies of the ai-wiki skills: fixed pointers to the installed ones.

Multica copies each agent's mounted workspace skill into the task directory and never reads ``~/.agents``, so a full
copy there lags every ``ai-wiki update`` until someone republishes it. A pointer keeps only the skill's frontmatter
(its trigger) and tells the agent to update the CLI and then read the installed skill, which the CLI keeps in step.
``--check`` (and CI) keep the committed pointers equal to this generator. Republish
``multica/<name>/SKILL.md`` with ``multica skill update <id> --content-file`` only when a release changes a file
under ``multica/`` or a read-back of the workspace copy differs from it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aiwiki.skills import NAMES  # noqa: E402

LEASE_NOTE = {
    "ai-wiki": "never between `maint begin` or `review begin` and its `end`",
    "ai-wiki-curating-maintainer": "never between `maint begin` and `maint end`",
    "ai-wiki-auditor": "never between `review begin` and `review end`",
    "okf-knowledge-curator": "never during a `maint` or `review` run",
}


def frontmatter(name: str) -> str:
    text = (ROOT / "skills" / name / "SKILL.md").read_text("utf-8")
    match = re.match(r"---\n.*?\n---\n", text, re.S)
    if not match:
        raise SystemExit(f"skills/{name}/SKILL.md has no frontmatter")
    return match.group(0)


def pointer(name: str) -> str:
    return f"""{frontmatter(name)}
# {name}

This copy is a pointer. The real instructions ship with the `ai-wiki` CLI on this machine and update with it, so
they are always the version that matches the CLI.

1. Run `ai-wiki update --check`. If it shows a `help` row (`available: true` or skills not `current`), run
   `ai-wiki update && hash -r` ({LEASE_NOTE[name]}). A failed check or update: say so in one line, never retry it
   or install by hand, and go on only if `ai-wiki skill` shows `{name}` `current`.
2. Run `ai-wiki skill`, read the `skill_md` it lists for `{name}`, and follow that file, not this one.
3. If `ai-wiki` is not installed or that file cannot be read, stop blocked:
   "ai-wiki is not installed on this runtime". Never copy skills by hand.
"""


def main() -> int:
    stale = []
    for name in NAMES:
        path = ROOT / "multica" / name / "SKILL.md"
        text = pointer(name)
        if "--check" in sys.argv[1:]:
            if not path.is_file() or path.read_text("utf-8") != text:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("stale (run python3 scripts/multica_pointers.py):", ", ".join(stale), file=sys.stderr)
        return 1
    if "--check" in sys.argv[1:]:
        print("multica pointers up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
