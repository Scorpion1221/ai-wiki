#!/usr/bin/env python3
"""Backfill structured OKF v0.2 `sources:` entries on concepts that lack them.

`sources: [{id, resource, title}]` is the durable provenance spine that lets
scan_sources.py map drift to concepts exactly. This seeds it from what each
concept already references:

- If the bundle has exactly ONE source snapshot, every concept missing `sources:`
  gets it (in a single-source bundle all concepts derive from it).
- Otherwise, match by filename stem appearing in the concept body, and add the
  matched source(s). Concepts with no match are reported, not guessed.

The block is inserted after `source_ref:` (line-based) so the rest of the
frontmatter is preserved verbatim. Concepts with valid structured sources are
skipped. Legacy string sources are an error rather than silently upgraded.

Deterministic, Python3 + PyYAML + stdlib only.

Usage:
    backfill_sources.py <bundle> [--write]      # default: dry-run preview
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SKIP_TOP = {"sources", ".okf"}
DELIM = "---"
_MIN_STEM = 4
_LEGACY_FIELDS = {"timestamp", "last_verified_at"}
_LEGACY_STATUSES = {"reviewed", "canonical", "stale"}


def _source_rels(root: Path):
    sdir = root / "sources"
    if sdir.is_symlink():
        raise ValueError("sources: source directory must not be a symlink")
    if not sdir.is_dir():
        return []
    out = []
    for directory, dirnames, filenames in os.walk(sdir, followlinks=False):
        base = Path(directory)
        safe_dirs: list[str] = []
        for name in sorted(dirnames):
            path = base / name
            rel = path.relative_to(sdir)
            if path.is_symlink():
                raise ValueError(
                    f"{path.relative_to(root).as_posix()}: source snapshots must not be symlinks"
                )
            if "inbox" in rel.parts:
                continue
            safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(sdir)
            if path.is_symlink():
                raise ValueError(
                    f"{path.relative_to(root).as_posix()}: source snapshots must not be symlinks"
                )
            if "inbox" in rel.parts or name == ".hashes.yaml":
                continue
            if path.is_file():
                out.append(path.relative_to(root).as_posix())
    return out


def _concepts(root: Path) -> list[tuple[Path, str]]:
    """Enumerate concepts without following a symlinked file or ancestor."""
    concepts: list[tuple[Path, str]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        safe_dirs: list[str] = []
        for name in sorted(dirnames):
            path = base / name
            rel = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(
                    f"{rel.as_posix()}: concept paths must not contain symlinks"
                )
            if name == ".git" or (len(rel.parts) == 1 and name in SKIP_TOP):
                continue
            safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(
                    f"{rel.as_posix()}: concept paths must not contain symlinks"
                )
            if path.suffix == ".md" and path.name not in RESERVED and path.is_file():
                concepts.append((path, rel.as_posix()))
    return sorted(concepts, key=lambda item: item[1])


def _fm_bounds(lines):
    if not lines or lines[0].strip() != DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            return 1, i  # fm body is lines[1:i]
    return None


def _parse_frontmatter(lines: list[str]) -> dict:
    bounds = _fm_bounds(lines)
    if not bounds:
        raise ValueError("missing or unterminated YAML frontmatter")
    start, end = bounds
    try:
        fm = yaml.safe_load("\n".join(lines[start:end])) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    return fm


def _validate_existing_sources(value) -> None:
    if not isinstance(value, list):
        raise ValueError("sources must be a list of mappings")
    for i, source in enumerate(value):
        if not isinstance(source, dict):
            raise ValueError(
                f"sources[{i}] must be a mapping with resource; legacy string sources are not supported"
            )
        resource = source.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise ValueError(f"sources[{i}].resource must be a non-empty string")


def _reject_legacy(fm: dict, text: str) -> None:
    legacy_fields = sorted(_LEGACY_FIELDS & fm.keys())
    if legacy_fields:
        raise ValueError(f"legacy fields are not supported: {', '.join(legacy_fields)}")
    status = fm.get("status")
    if status in _LEGACY_STATUSES:
        raise ValueError(f"legacy status {status!r} is not supported")
    if any(line.strip() == "# Citations" for line in text.splitlines()):
        raise ValueError("legacy '# Citations' section is not supported")


def _source_id(rel: str) -> str:
    raw = rel.removeprefix("sources/")
    if raw.endswith(".source"):
        raw = raw.removesuffix(".source")
    raw = str(Path(raw).with_suffix(""))
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "source"


def _source_title(rel: str) -> str:
    name = Path(rel).name
    if name.endswith(".source"):
        name = name.removesuffix(".source")
    return Path(name).stem or name


def _source_specs(rels: list[str]) -> dict[str, dict[str, str]]:
    specs = {}
    used: dict[str, str] = {}
    for rel in rels:
        source_id = _source_id(rel)
        if source_id in used and used[source_id] != rel:
            source_id = f"{source_id}-{hashlib.sha256(rel.encode()).hexdigest()[:8]}"
        used[source_id] = rel
        specs[rel] = {
            "id": source_id,
            "resource": f"/{rel}",
            "title": _source_title(rel),
        }
    return specs


def _source_block(entries: list[dict[str, str]]) -> list[str]:
    dumped = yaml.safe_dump(
        {"sources": entries},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return dumped.rstrip().splitlines()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backfill structured `sources:` on OKF v0.2 concepts.")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--write", action="store_true", help="apply changes (default: dry-run)")
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")

    try:
        sources = _source_rels(root)
    except ValueError as exc:
        print(f"error: unsafe source tree: {exc}", file=sys.stderr)
        return 2
    if not sources:
        print("no source snapshots under sources/ — nothing to link")
        return 0
    try:
        concepts = _concepts(root)
    except ValueError as exc:
        print(f"error: unsafe concept tree: {exc}", file=sys.stderr)
        return 2
    single = sources[0] if len(sources) == 1 else None
    stems = {source: Path(source).stem for source in sources}
    specs = _source_specs(sources)

    added, skipped, unmatched, invalid = [], [], [], []
    for path, rel in concepts:
        lines = path.read_text(encoding="utf-8").splitlines()
        bounds = _fm_bounds(lines)
        if not bounds:
            invalid.append((rel, "missing or unterminated YAML frontmatter"))
            continue
        start, end = bounds
        try:
            fm = _parse_frontmatter(lines)
            _reject_legacy(fm, "\n".join(lines))
        except ValueError as exc:
            invalid.append((rel, str(exc)))
            continue
        if "sources" in fm:
            try:
                _validate_existing_sources(fm["sources"])
            except ValueError as exc:
                invalid.append((rel, str(exc)))
                continue
            skipped.append(rel)
            continue

        text = "\n".join(lines)
        if single:
            matched = [single]
        else:
            matched = [
                source
                for source in sources
                if len(stems[source]) >= _MIN_STEM and stems[source] in text
            ]
        if not matched:
            unmatched.append((rel, "no source stem found in body"))
            continue

        entries = [specs[source] for source in matched]
        block = _source_block(entries)
        # Insert after source_ref:, else after the last source_* extension, else
        # immediately before the frontmatter close delimiter.
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
        added.append((path, rel, entries, lines, insert_at, block))

    # Preflight the whole bundle before writing, so a legacy concept cannot cause
    # a partial migration.
    if invalid:
        for rel, why in invalid:
            print(f"error: {rel}: {why}", file=sys.stderr)
        print(
            f"error: refused to backfill with {len(invalid)} invalid OKF v0.2 concept(s)",
            file=sys.stderr,
        )
        return 2

    if a.write:
        for path, _rel, _entries, lines, insert_at, block in added:
            lines[insert_at:insert_at] = block
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verb = "added" if a.write else "would add"
    print(
        f"{verb} sources to {len(added)} concept(s); skipped {len(skipped)} "
        f"(already structured); {len(unmatched)} unmatched"
    )
    for _path, rel, entries, _lines, _insert_at, _block in added[:8]:
        resources = ", ".join(entry["resource"] for entry in entries)
        print(f"  + {rel}: {resources}")
    if len(added) > 8:
        print(f"  … +{len(added) - 8} more")
    for rel, why in unmatched:
        print(f"  ? {rel}: {why}")
    if not a.write and added:
        print("\n(dry-run — re-run with --write to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
