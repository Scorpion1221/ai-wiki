#!/usr/bin/env python3
"""Append a parseable entry to a bundle's log.md (the chronological update ledger).

OKF SPEC §7 / Karpathy LLM-wiki pattern: log.md is an append-only, grep-parseable
record of what happened and when. Each entry looks like:

    ## [YYYY-MM-DD] <op> | <subject>
    - <note line>
    - files: a.md, b.md

so `grep "^## \\[" log.md | tail -20` gives recent activity at a glance. When log.md
passes 500 entries it is rotated into log-<YYYY>.md, keeping the live file small.

Deterministic, stdlib only.

Usage:
    append_log.py <bundle> <op> "<subject>" [--files f1 f2 ...] [--note "..."] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

OPS = ["ingest", "query", "update", "merge", "delete", "lint", "create", "note"]
HEADER = ('# Update Log\n\nAppend-only, newest entries at the bottom. '
          'Parse recent activity with `grep "^## \\[" log.md | tail`.\n')
ROTATE_AT = 500


def _entry_count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith("## ["))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Append an entry to a bundle's log.md.")
    p.add_argument("bundle", type=Path)
    p.add_argument("op", help=f"operation (suggested: {', '.join(OPS)})")
    p.add_argument("subject")
    p.add_argument("--files", nargs="*", default=[])
    p.add_argument("--note", default="")
    p.add_argument("--date", default=None, help="override date YYYY-MM-DD (default: today)")
    a = p.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        p.error(f"not a bundle directory: {root}")
    log = root / "log.md"
    today = a.date or date.today().isoformat()

    text = log.read_text(encoding="utf-8") if log.exists() else HEADER

    # Rotate the live log when it grows past the cap.
    if _entry_count(text) >= ROTATE_AT:
        year = today[:4]
        archive = root / f"log-{year}.md"
        body = text[len(HEADER):] if text.startswith(HEADER) else text
        prev = (archive.read_text(encoding="utf-8")
                if archive.exists() else f"# Update Log {year} (archived)\n")
        archive.write_text(prev.rstrip() + "\n\n" + body.strip() + "\n", encoding="utf-8")
        text = HEADER
        print(f"rotated {ROTATE_AT}+ entries into log-{year}.md")

    lines = [f"\n## [{today}] {a.op} | {a.subject}"]
    for nl in a.note.splitlines():
        if nl.strip():
            lines.append(f"- {nl}")
    if a.files:
        lines.append("- files: " + ", ".join(a.files))
    entry = "\n".join(lines) + "\n"

    log.write_text(text.rstrip() + "\n" + entry, encoding="utf-8")
    print(f"appended to {log.relative_to(root)}: [{today}] {a.op} | {a.subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
