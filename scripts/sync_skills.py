#!/usr/bin/env python3
"""Check or synchronize repository-owned AI Wiki skills into a runtime.

The destination may contain a platform-managed ``multica-metadata.json``; it is ignored
during checks and preserved on apply. Every other destination file must come from the
repository so deleted/renamed skill resources cannot linger as hidden behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

SKILLS = ("ai-wiki", "ai-wiki-maintainer", "okf-knowledge-curator")
PRESERVE = {"multica-metadata.json"}


def _files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in PRESERVE or "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _check(source: Path, target: Path) -> list[str]:
    expected, actual = _files(source), _files(target)
    findings: list[str] = []
    for rel in sorted(expected.keys() - actual.keys()):
        findings.append(f"missing {rel}")
    for rel in sorted(actual.keys() - expected.keys()):
        findings.append(f"extra {rel}")
    for rel in sorted(expected.keys() & actual.keys()):
        if expected[rel] != actual[rel]:
            findings.append(f"changed {rel}")
    return findings


def _apply(source: Path, target: Path) -> None:
    is_link = target.is_symlink()
    preserved = {} if is_link else {
        name: (target / name).read_bytes()
        for name in PRESERVE
        if (target / name).is_file()
    }
    if is_link:
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    for name, content in preserved.items():
        (target / name).write_bytes(content)


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift (default)")
    mode.add_argument("--apply", action="store_true", help="replace runtime copies")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(os.environ.get("AIWIKI_SKILLS_HOME", "~/.agents/skills")).expanduser(),
        help="runtime skills directory (default: ~/.agents/skills)",
    )
    parser.add_argument("skills", nargs="*", choices=SKILLS, help="subset (default: all)")
    args = parser.parse_args(argv)

    names = args.skills or list(SKILLS)
    failed = False
    for name in names:
        source, target = repo / "skills" / name, args.dest.expanduser() / name
        if not source.is_dir():
            print(f"ERROR {name}: repository source missing: {source}")
            failed = True
            continue
        if args.apply:
            _apply(source, target)
        findings = _check(source, target)
        if findings:
            failed = True
            print(f"DRIFT {name}: " + ", ".join(findings))
        else:
            print(f"OK {name}: {target}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
