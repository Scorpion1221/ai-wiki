#!/usr/bin/env python3
"""Detect source drift in an OKF bundle and report concepts that need re-verification.

sources/ holds immutable raw-source snapshots. This hashes each (body-only for
markdown, raw bytes otherwise), diffs against sources/.hashes.yaml, and reports
NEW / CHANGED / DELETED sources plus the concepts that cite each changed/deleted
source — the candidates for `status: stale`. This is what wires up the otherwise
dormant `status: stale` / `source_updated_at` / `last_verified_at` fields.

COMMIT-AFTER-SUCCESS (important): default is READ-ONLY (report). Pass --commit to
write current hashes into sources/.hashes.yaml. Only --commit AFTER the curating
agent has actually re-verified/updated the affected concepts. Committing before
re-curation silently marks the drift as "seen" and loses it.

Deterministic, Python3 + PyYAML + stdlib only.

Usage:
    scan_sources.py <bundle> [--report] [--json] [--commit]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

SOURCES = "sources"
HASHES = "sources/.hashes.yaml"
SKIP_TOP = {"sources", ".okf"}
RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
DELIM = "---"
_TEXT_EXT = {".md", ".mdx", ".markdown", ".txt"}


def _body_only(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == DELIM:
        for i in range(1, len(lines)):
            if lines[i].strip() == DELIM:
                return "\n".join(lines[i + 1:])
    return text


def _hash_file(p: Path) -> str:
    if p.suffix.lower() in _TEXT_EXT:
        body = _body_only(p.read_text(encoding="utf-8", errors="replace"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        return {}
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}
            return fm if isinstance(fm, dict) else {}
    return {}


def _current_sources(root: Path) -> dict:
    out = {}
    sdir = root / SOURCES
    if not sdir.is_dir():
        return out
    for p in sorted(sdir.rglob("*")):
        # Hash every snapshot regardless of name (a source may legitimately be named
        # index.md / SCHEMA.md); only skip the sidecar and the not-yet-ingested inbox.
        if not p.is_file() or p.name == ".hashes.yaml":
            continue
        if "inbox" in p.relative_to(sdir).parts:  # drop-zone, not yet a snapshot
            continue
        out[p.relative_to(root).as_posix()] = _hash_file(p)
    return out


def _concepts(root: Path):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if p.name in RESERVED or p.name.startswith("log-") or rel.parts[0] in SKIP_TOP:
            continue
        yield p, rel.as_posix()


_MIN_STEM = 6  # below this a filename stem is too generic for the substring fallback


def _affected(root: Path, changed_rels: set):
    """source rel-path -> [concept rel-paths citing it], plus whether the (imprecise)
    stem fallback was used. Prefers frontmatter `sources:` exact match; only falls back
    to a body substring on the source filename stem for concepts that declare NO
    `sources:` (report-only heuristic, never used for rewriting), and only for stems
    long enough (>= 6 chars) to avoid matching common short tokens."""
    stems = {rel: Path(rel).stem for rel in changed_rels}
    hits = {rel: [] for rel in changed_rels}
    used_fallback = False
    for p, crel in _concepts(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        srcs = _frontmatter(text).get("sources") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        srcs = {str(s) for s in srcs}
        for rel in changed_rels:
            if rel in srcs:
                hits[rel].append(crel)
            elif not srcs and len(stems[rel]) >= _MIN_STEM and stems[rel] in text:
                hits[rel].append(crel)
                used_fallback = True
    return hits, used_fallback


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Detect source drift; report affected concepts.")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--report", action="store_true", help="(default) print a human report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--commit", action="store_true",
                    help="write current hashes (ONLY after re-curation succeeds)")
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")

    hpath = root / HASHES
    prior = {}
    if hpath.exists():
        loaded = yaml.safe_load(hpath.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            prior = loaded

    cur = _current_sources(root)
    new = sorted(set(cur) - set(prior))
    deleted = sorted(set(prior) - set(cur))
    changed = sorted(r for r in (set(cur) & set(prior)) if cur[r] != prior[r])
    affected, used_fallback = _affected(root, set(changed) | set(deleted))
    affected = {k: v for k, v in affected.items() if v}

    if a.json:
        print(json.dumps({"new": new, "changed": changed, "deleted": deleted,
                          "affected_concepts": affected}, ensure_ascii=False, indent=2))
    elif not (new or changed or deleted):
        tail = "" if prior else " (no baseline yet — run --commit to establish one)"
        print(f"✓ no source drift{tail}")
    else:
        if new:
            print(f"NEW ({len(new)}):")
            for r in new:
                print(f"  + {r}")
        if changed:
            print(f"CHANGED ({len(changed)}):")
            for r in changed:
                print(f"  ~ {r}")
        if deleted:
            print(f"DELETED ({len(deleted)}):")
            for r in deleted:
                print(f"  - {r}")
        if affected:
            print("\nConcepts to re-verify (candidates for status: stale):")
            for src, cs in affected.items():
                print(f"  {src}:")
                for c in cs:
                    print(f"      → {c}")
            if used_fallback:
                print("\n  (some matches used a filename-stem heuristic — add "
                      "`sources: [sources/<file>]` frontmatter for precise mapping)")

    if a.commit:
        hpath.parent.mkdir(parents=True, exist_ok=True)
        hpath.write_text(yaml.safe_dump(cur, sort_keys=True, allow_unicode=True), encoding="utf-8")
        print(f"\n✓ committed {len(cur)} source hash(es) to {HASHES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
