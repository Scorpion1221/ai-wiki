#!/usr/bin/env python3
"""Append an entry to an OKF v0.2 date-grouped ``log.md`` ledger.

OKF v0.2 log files use one ISO date heading per day and flat prose bullets,
newest first::

    # Update Log

    ## 2026-08-13
    * **Ingest**: Added WAIO-68 source and refreshed two concepts.
    * **Audit**: Verified the generated concepts against the source snapshot.

Legacy ``## [date] operation`` headings are rejected rather than silently mixed
with the v0.2 format.

Usage:
    append_log.py <bundle> <op> "<subject>" [--files f1 f2 ...] [--note "..."] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from aiwiki.engine.document import has_symlink_component

OPS = ["ingest", "audit", "query", "update", "merge", "delete", "lint", "create", "note"]
HEADER = "# Update Log\n\nAppend-only ledger, newest entries first.\n"
_DATE_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")


def _valid_date(raw: str) -> str:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date {raw!r}; expected YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"invalid ISO date {raw!r}; expected YYYY-MM-DD")
    return raw


def _parse_log(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Return the intro and unique date sections; reject non-v0.2 h2 headings."""
    lines = text.splitlines()
    starts = []
    seen = set()
    for i, line in enumerate(lines):
        if not line.startswith("##"):
            continue
        match = _DATE_HEADING.fullmatch(line)
        if not match:
            raise ValueError(
                f"line {i + 1}: log headings must be '## YYYY-MM-DD'; got {line!r}"
            )
        day = _valid_date(match.group(1))
        if day in seen:
            raise ValueError(f"line {i + 1}: duplicate date heading {day}; group the bullets")
        seen.add(day)
        starts.append((i, day))

    first = starts[0][0] if starts else len(lines)
    intro = lines[:first]
    sections = {}
    for n, (start, day) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = lines[start + 1:end]
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        sections[day] = body
    return intro, sections


def _render_log(intro: list[str], sections: dict[str, list[str]]) -> str:
    prefix = "\n".join(intro).rstrip() or HEADER.rstrip()
    chunks = [prefix]
    for day in sorted(sections, reverse=True):
        body = "\n".join(sections[day]).strip()
        chunks.append(f"## {day}" + (f"\n{body}" if body else ""))
    return "\n\n".join(chunks).rstrip() + "\n"


def _entry(op: str, subject: str, note: str, files: list[str]) -> str:
    label = op.strip().replace("-", " ").title()
    if not label:
        raise ValueError("operation must not be empty")
    clean_subject = " ".join(subject.split())
    if not clean_subject:
        raise ValueError("subject must not be empty")
    segments = [f"* **{label}**: {clean_subject}"]
    clean_note = " ".join(note.split())
    if clean_note:
        segments.append(clean_note)
    if files:
        segments.append("files: " + ", ".join(files))
    return " — ".join(segments)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Append an entry to an OKF v0.2 log.md.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("op", help=f"operation (suggested: {', '.join(OPS)})")
    parser.add_argument("subject")
    parser.add_argument("--files", nargs="*", default=[])
    parser.add_argument("--note", default="")
    parser.add_argument("--date", default=None, help="override date YYYY-MM-DD (default: today)")
    args = parser.parse_args(argv)

    root = args.bundle.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a bundle directory: {root}")
    log = root / "log.md"
    if has_symlink_component(root, log):
        print("error: unsafe log path (outside bundle or symlink): log.md", file=sys.stderr)
        return 2

    try:
        today = _valid_date(args.date or date.today().isoformat())
        entry = _entry(args.op, args.subject, args.note, args.files)
        text = log.read_text(encoding="utf-8") if log.exists() else HEADER
        intro, sections = _parse_log(text)
    except ValueError as exc:
        print(f"error: invalid OKF v0.2 log: {exc}", file=sys.stderr)
        return 2

    # Newest operation first within the day; date sections are sorted newest first
    # by _render_log.
    sections[today] = [entry, *sections.get(today, [])]
    log.write_text(_render_log(intro, sections), encoding="utf-8")
    print(f"appended to {log.relative_to(root)}: {today} {args.op} | {args.subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
