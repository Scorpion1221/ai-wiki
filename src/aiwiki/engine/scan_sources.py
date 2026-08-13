#!/usr/bin/env python3
"""Detect source drift in an OKF v0.2 bundle and report concepts to re-verify.

sources/ holds immutable raw-source snapshots. This hashes the exact bytes of
each snapshot, diffs against sources/.hashes.yaml, and reports
NEW / CHANGED / DELETED sources plus the concepts whose structured
`sources[].resource` points at each changed/deleted snapshot. Matching is exact;
there is no body-text or filename-stem fallback.

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
import os
import posixpath
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

SOURCES = "sources"
HASHES = "sources/.hashes.yaml"
SKIP_TOP = {"sources", ".okf"}
RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
DELIM = "---"
_LEGACY_FIELDS = {"timestamp", "last_verified_at"}
_LEGACY_STATUSES = {"reviewed", "canonical", "stale"}


def _hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        raise ValueError("missing YAML frontmatter")
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
            if not isinstance(fm, dict):
                raise ValueError("frontmatter is not a mapping")
            return fm
    raise ValueError("unterminated YAML frontmatter")


def _current_sources(root: Path) -> dict:
    out = {}
    sdir = root / SOURCES
    if sdir.is_symlink():
        raise ValueError("sources: source directory must not be a symlink")
    if not sdir.is_dir():
        return out
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
            if "inbox" in rel.parts:  # operational drop-zone, not a snapshot
                continue
            safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(sdir)
            # Check before is_file(): is_file() follows a symlink and could make an
            # out-of-bundle target eligible for hashing.
            if path.is_symlink():
                raise ValueError(
                    f"{path.relative_to(root).as_posix()}: source snapshots must not be symlinks"
                )
            if "inbox" in rel.parts or name == ".hashes.yaml":
                continue
            if path.is_file():
                out[path.relative_to(root).as_posix()] = _hash_file(path)
    return out


def _concepts(root: Path) -> list[tuple[Path, str]]:
    """Enumerate concepts without following a symlinked file or ancestor."""
    # Check the source root first so a symlinked ``sources/`` reports the
    # provenance-specific error before the generic bundle walker sees it.
    source_root = root / SOURCES
    if source_root.is_symlink():
        raise ValueError("sources: source directory must not be a symlink")
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


def _source_resource_rel(resource: str, concept_rel: str) -> str | None:
    """Resolve an OKF resource to a bundle-relative path, or None if external."""
    parsed = urlsplit(resource)
    if parsed.scheme or parsed.netloc:
        return None
    raw = parsed.path
    if not raw:
        return None
    if raw.startswith("/"):
        candidate = raw.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(concept_rel), raw)
    normalized = posixpath.normpath(candidate)
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized.removeprefix("./")


def _structured_resources(value, *, concept_rel: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ValueError("sources must be a list of mappings")
    resources = set()
    for i, source in enumerate(value):
        if not isinstance(source, dict):
            raise ValueError(
                f"sources[{i}] must be a mapping with resource; legacy string sources are not supported"
            )
        resource = source.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise ValueError(f"sources[{i}].resource must be a non-empty string")
        rel = _source_resource_rel(resource.strip(), concept_rel)
        if rel is not None:
            resources.add(rel)
    return resources


def _reject_legacy(fm: dict, text: str) -> None:
    legacy_fields = sorted(_LEGACY_FIELDS & fm.keys())
    if legacy_fields:
        raise ValueError(f"legacy fields are not supported: {', '.join(legacy_fields)}")
    status = fm.get("status")
    if status in _LEGACY_STATUSES:
        raise ValueError(f"legacy status {status!r} is not supported")
    if any(line.strip() == "# Citations" for line in text.splitlines()):
        raise ValueError("legacy '# Citations' section is not supported")


def _affected(root: Path, changed_rels: set):
    """Map changed source paths to concepts via exact sources[].resource paths."""
    hits = {rel: [] for rel in changed_rels}
    for p, crel in _concepts(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            fm = _frontmatter(text)
            srcs = _structured_resources(fm.get("sources"), concept_rel=crel)
        except ValueError as exc:
            raise ValueError(f"{crel}: {exc}") from exc
        for rel in changed_rels:
            if rel in srcs:
                hits[rel].append(crel)
    return hits


def _preflight_concepts(root: Path) -> None:
    """Reject legacy sources even on a no-drift run.

    A migration-only CLI must never appear healthy merely because the hash
    baseline happens to match while concepts still use v0.1 string sources.
    """
    for p, crel in _concepts(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            fm = _frontmatter(text)
            _reject_legacy(fm, text)
            _structured_resources(fm.get("sources"), concept_rel=crel)
        except ValueError as exc:
            raise ValueError(f"{crel}: {exc}") from exc


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

    try:
        _preflight_concepts(root)
    except ValueError as exc:
        print(f"error: invalid OKF v0.2 concept: {exc}", file=sys.stderr)
        return 2

    try:
        cur = _current_sources(root)
    except ValueError as exc:
        print(f"error: unsafe source tree: {exc}", file=sys.stderr)
        return 2

    # Validate the source tree before reading the hash ledger: the ledger lives
    # under sources/ too and must never be allowed to point outside the bundle.
    hpath = root / HASHES
    prior = {}
    if hpath.exists():
        loaded = yaml.safe_load(hpath.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            prior = loaded
    new = sorted(set(cur) - set(prior))
    deleted = sorted(set(prior) - set(cur))
    changed = sorted(r for r in (set(cur) & set(prior)) if cur[r] != prior[r])
    try:
        affected = _affected(root, set(changed) | set(deleted))
    except ValueError as exc:
        print(f"error: invalid OKF v0.2 concept: {exc}", file=sys.stderr)
        return 2
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
            print("\nConcepts to re-verify (source drift detected):")
            for src, cs in affected.items():
                print(f"  {src}:")
                for c in cs:
                    print(f"      → {c}")

    if a.commit:
        hpath.parent.mkdir(parents=True, exist_ok=True)
        hpath.write_text(yaml.safe_dump(cur, sort_keys=True, allow_unicode=True), encoding="utf-8")
        print(f"\n✓ committed {len(cur)} source hash(es) to {HASHES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
