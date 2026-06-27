#!/usr/bin/env python3
"""Generate per-directory index.md files for an OKF bundle (progressive disclosure).

Every directory carries an index.md so a human or agent can drill in one level
at a time. Index files have NO frontmatter (OKF SPEC §6). Each index leads with
a prose description of what the directory (or, at the root, the whole bundle)
contains — so a retriever knows the scope before opening any concept — then
lists entries grouped by section:

    <directory description>

    # <Concept Type>
    * [Title](file.md) - description        # description from the concept's frontmatter

    # Subdirectories
    * [subdir](subdir/index.md) - description   # what that subdirectory contains

Where descriptions come from:
  * Concept entries  -> the concept's own frontmatter `description`.
  * A directory      -> `index-meta.yaml` at the bundle root, else (for a
                        single-concept directory) that one concept's description,
                        matching the OKF reference agent's rule.
  * The whole bundle -> `index-meta.yaml` `title` + `description`.

This keeps the generator deterministic and PyYAML-only: the curating agent
authors the directory/bundle prose in index-meta.yaml (just as it authors each
concept's frontmatter), and the script assembles every level mechanically.
Raw snapshots under sources/ are not indexed as concepts; they are listed from
the root index under a Sources section.

index-meta.yaml (all keys optional):

    title: Human bundle title
    description: One-paragraph overview of the whole bundle.
    directories:
      features: One sentence on what features/ collectively contains.
      monetization: ...

Deterministic and idempotent — re-run after the bundle changes.

Usage:
    python3 gen_indexes.py <bundle-dir>
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SOURCES_DIR = "sources"
META_FILE = "index-meta.yaml"
_DELIM = "---"


def _parse_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIM:
        return {}
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}
            return fm if isinstance(fm, dict) else {}
    return {}


def _meta(path: Path) -> dict[str, str]:
    fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return {
        "title": str(fm.get("title") or path.stem),
        "description": str(fm.get("description") or ""),
        "type": str(fm.get("type") or "Other"),
    }


def _is_concept(p: Path, root: Path) -> bool:
    if not p.is_file() or p.suffix != ".md" or p.name in RESERVED or p.name.startswith("log-"):
        return False
    parts = p.relative_to(root).parts
    return SOURCES_DIR not in parts and ".okf" not in parts


def _load_sidecar(root: Path) -> dict[str, Any]:
    f = root / META_FILE
    if not f.exists():
        return {}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _dirs_to_index(root: Path) -> set[Path]:
    """Root plus every ancestor directory that (recursively) holds a concept."""
    dirs = {root}
    for p in root.rglob("*.md"):
        if not _is_concept(p, root):
            continue
        d = p.parent
        while True:
            dirs.add(d)
            if d == root:
                break
            d = d.parent
    return dirs


def _grouped_sections(entries: list[tuple[str, str, str, str]]) -> str:
    """entries: (group, title, link, description) -> '# Group' sections."""
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for group, title, link, desc in entries:
        grouped[group].append((title, link, desc))
    out: list[str] = []
    for group in sorted(grouped):
        lines = [f"# {group}", ""]
        for title, link, desc in sorted(grouped[group], key=lambda e: e[0].lower()):
            lines.append(f"* [{title}]({link})" + (f" - {desc}" if desc else ""))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def generate_indexes(root: Path) -> tuple[list[Path], list[str]]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Bundle directory not found: {root}")

    sidecar = _load_sidecar(root)
    overrides = sidecar.get("directories") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    bundle_title = str(sidecar.get("title") or "")
    bundle_desc = str(sidecar.get("description") or "")

    index_dirs = _dirs_to_index(root)
    # Deepest-first so a parent can reuse a child's resolved description.
    ordered = sorted(index_dirs,
                     key=lambda p: (-len(p.relative_to(root).parts), str(p)))

    dir_desc: dict[Path, str] = {}
    written: list[Path] = []
    missing: list[str] = []

    for d in ordered:
        is_root = d == root
        rel = "" if is_root else str(d.relative_to(root)).replace("\\", "/")

        entries: list[tuple[str, str, str, str]] = []
        for child in sorted(d.iterdir()):
            if child.is_file() and _is_concept(child, root):
                m = _meta(child)
                entries.append((m["type"], m["title"], child.name, m["description"]))
            elif child.is_dir() and child in index_dirs:
                entries.append(("Subdirectories", child.name,
                                f"{child.name}/index.md", dir_desc.get(child, "")))

        # Resolve this directory's own description (used as its lead paragraph
        # and as its entry's description in the parent index).
        if not is_root:
            concept_descs = [desc for grp, _, _, desc in entries
                             if grp != "Subdirectories"]
            override = str(overrides.get(rel, "")).strip()
            if override:
                dir_desc[d] = override
            elif len(concept_descs) == 1 and concept_descs[0]:
                dir_desc[d] = concept_descs[0]
            else:
                dir_desc[d] = ""
                if len(concept_descs) > 1:
                    missing.append(rel)

        # Assemble: lead description, grouped sections, then (root) Sources.
        parts: list[str] = []
        if is_root:
            if bundle_title:
                parts.append(f"# {bundle_title}")
            if bundle_desc:
                parts.append(bundle_desc)
        elif dir_desc.get(d):
            parts.append(dir_desc[d])

        body = _grouped_sections(entries)
        if body:
            parts.append(body)

        if is_root:
            src_dir = root / SOURCES_DIR
            if src_dir.is_dir():
                src_files = sorted(p for p in src_dir.glob("*.md")
                                   if p.name not in RESERVED)
                if src_files:
                    lines = ["# Sources", ""]
                    for p in src_files:
                        lines.append(f"* [{p.stem}]({SOURCES_DIR}/{p.name}) "
                                     f"- raw source snapshot")
                    parts.append("\n".join(lines))

        if len(parts) == 0:
            continue
        (d / "index.md").write_text("\n\n".join(parts).rstrip() + "\n",
                                    encoding="utf-8")
        written.append(d / "index.md")

    return written, missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate per-directory index.md files for an OKF bundle.")
    p.add_argument("bundle", type=Path, help="Path to the bundle root directory.")
    args = p.parse_args(argv)
    root = args.bundle.expanduser().resolve()
    written, missing = generate_indexes(root)
    print(f"✓ wrote {len(written)} index.md file(s):")
    for w in written:
        print(f"  {w.relative_to(root)}")
    if missing:
        print(f"\n⚠ {len(missing)} multi-concept director(ies) have no description "
              f"in {META_FILE}; add a 'directories:' entry for each:",
              file=sys.stderr)
        for rel in missing:
            print(f"  {rel}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
