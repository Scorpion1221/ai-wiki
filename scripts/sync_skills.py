#!/usr/bin/env python3
"""Check or synchronize repository-owned AI Wiki skills into a runtime.

The destination may contain a platform-managed ``multica-metadata.json``; it is ignored
during checks and preserved on apply. Every other destination file must come from the
repository so deleted/renamed skill resources cannot linger as hidden behavior.

``--apply`` first runs a skill's package shims (``scripts/*.py`` over ``_aiwiki``) from a
copy outside the checkout, as a scheduled agent would, and leaves the runtime copy alone if
one fails: the shims need an installed ai-wiki CLI with ``aiwiki.maint``, so the CLI must be
upgraded before the skill is synced.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILLS = ("ai-wiki", "ai-wiki-maintainer", "ai-wiki-curating-maintainer", "okf-knowledge-curator")
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


def _shim_failure(source: Path) -> str | None:
    """Why the skill's shims fail when run as deployed (outside a checkout, PATH's python3), or None."""
    shims = [path.name for path in sorted((source / "scripts").glob("*.py"))
             if "from _aiwiki import" in path.read_text(encoding="utf-8")]
    if not shims:
        return None
    python = shutil.which("python3")
    if python is None:
        return "python3 is not on PATH"
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "AIWIKI_MAINT_SHIM_REEXEC")}
    failing: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / source.name
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__"))
        for name in shims:
            result = subprocess.run([python, str(staged / "scripts" / name), "--help"], env=env, capture_output=True,
                                    text=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
            if result.returncode:
                failing.append((name, (result.stderr.strip().splitlines() or [f"exit {result.returncode}"])[-1]))
    if not failing:
        return None
    return f"{', '.join(name for name, _ in failing)} fail in this runtime: {failing[0][1]}"


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
            failure = _shim_failure(source)
            if failure:
                print(f"ERROR {name}: not applied; {failure}")
                failed = True
                continue
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
