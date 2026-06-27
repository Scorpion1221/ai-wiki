#!/usr/bin/env python3
"""Backfill the `sources:` frontmatter link on concepts that lack it.

`sources: [sources/<file>]` is the durable provenance spine that lets
scan_sources.py map drift to concepts EXACTLY (instead of the fragile
filename-stem heuristic). This seeds it from what each concept already cites:

- If the bundle has exactly ONE source snapshot, every concept missing `sources:`
  gets it (in a single-source bundle all concepts derive from it).
- Otherwise, match by filename stem appearing in the concept body, and add the
  matched source(s). Concepts with no match are reported, not guessed.

The `sources:` line is inserted after `source_ref:` (line-based) so the rest of
the frontmatter is preserved verbatim — no reformatting. Idempotent: concepts
that already declare `sources:` are skipped.

Deterministic, Python3 + PyYAML + stdlib only.

Usage:
    backfill_sources.py <bundle> [--write]      # default: dry-run preview
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SKIP_TOP = {"sources", ".okf"}
DELIM = "---"
_SPECIAL = re.compile(r"""[:\[\]{}#,&*!|>'"%@`]""")
_HAS_SOURCES = re.compile(r"^\s*sources\s*:")
_MIN_STEM = 4


def _source_rels(root: Path):
    sdir = root / "sources"
    if not sdir.is_dir():
        return []
    out = []
    for p in sorted(sdir.rglob("*")):
        if not p.is_file() or p.name == ".hashes.yaml":
            continue
        if "inbox" in p.relative_to(sdir).parts:
            continue
        out.append(p.relative_to(root).as_posix())
    return out


def _concepts(root: Path):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if p.name in RESERVED or p.name.startswith("log-") or rel.parts[0] in SKIP_TOP:
            continue
        yield p, rel.as_posix()


def _fm_bounds(lines):
    if not lines or lines[0].strip() != DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            return 1, i  # fm body is lines[1:i]
    return None


def _yaml_path(rel: str) -> str:
    return f'"{rel}"' if _SPECIAL.search(rel) else rel


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill `sources:` frontmatter on concepts.")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--write", action="store_true", help="apply changes (default: dry-run)")
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")

    sources = _source_rels(root)
    if not sources:
        print("no source snapshots under sources/ — nothing to link")
        return 0
    single = sources[0] if len(sources) == 1 else None
    stems = {s: Path(s).stem for s in sources}

    added, skipped, unmatched = [], [], []
    for path, rel in _concepts(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        b = _fm_bounds(lines)
        if not b:
            unmatched.append((rel, "no frontmatter"))
            continue
        start, end = b
        if any(_HAS_SOURCES.match(ln) for ln in lines[start:end]):
            skipped.append(rel)
            continue

        text = "\n".join(lines)
        if single:
            matched = [single]
        else:
            matched = [s for s in sources
                       if len(stems[s]) >= _MIN_STEM and stems[s] in text]
        if not matched:
            unmatched.append((rel, "no source stem found in body"))
            continue

        value = "[" + ", ".join(_yaml_path(s) for s in matched) + "]"
        new_line = f"sources: {value}"
        # insert after source_ref:, else after the last source_* key, else before close
        anchor = None
        for i in range(start, end):
            if lines[i].startswith("source_ref:"):
                anchor = i
                break
        if anchor is None:
            for i in range(start, end):
                if lines[i].startswith("source"):
                    anchor = i
        insert_at = (anchor + 1) if anchor is not None else end
        added.append((rel, value))
        if a.write:
            lines.insert(insert_at, new_line)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verb = "added" if a.write else "would add"
    print(f"{verb} sources: to {len(added)} concept(s); skipped {len(skipped)} "
          f"(already have it); {len(unmatched)} unmatched")
    for rel, val in added[:8]:
        print(f"  + {rel}: sources: {val}")
    if len(added) > 8:
        print(f"  … +{len(added) - 8} more")
    for rel, why in unmatched:
        print(f"  ? {rel}: {why}")
    if not a.write and added:
        print("\n(dry-run — re-run with --write to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
