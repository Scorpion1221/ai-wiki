#!/usr/bin/env python3
"""May this release go to production without the owner's word? Stdlib only, read-only, no LLM.

The owner (2026-11-16) lets an agent merge and deploy a release by itself when the change cannot
alter what the writer stores, what the gate or the audit accepts, or how production is operated.
Everything else still waits for him. This script decides which a diff is, from the repository
alone, so the decision is not an agent's judgement:

    release_gate.py classify <repo-dir> <base> <head> [--json]

Exit 0: ``auto``, every changed file is on the low-risk list below. Exit 3: ``ask``, with the
files and the reason for each. Exit 2: the repository or a commit could not be read.

A changed file is low-risk when it is one of:

  tests/**                    tests and fixtures
  docs/** and README.md       documentation, but not docs/prompts/** (the autopilot prompts)
  a version stamp             src/aiwiki/version.py, pyproject.toml and uv.lock differing only in
                              the package version; a SKILL.md differing only in its stamp
  skills/ai-wiki/**           the reader's skill. The maintainer's, the curator's and the auditor's
                              skills are those agents' instructions: they ask, like a prompt
  src/aiwiki/**/*.py          a module outside the server packages that the server never imports
                              (see below) and that is neither the auditor's CLI nor the code that
                              freezes evidence

Everything else asks: a module the writer or the mirror imports, anything under the three server
packages, a new dependency, the deploy and watchdog scripts, CI, the Dockerfile, the prompts, the
Multica pointers, a symlink or submodule, any path this list does not name.

"The server imports it" is computed, not listed: the import closure of ``aiwiki.service``,
``aiwiki.runtime`` and ``aiwiki.engine`` inside the package, at the base and at the head, so a
module that joins or leaves the server in this very diff asks too. A package's ``__init__`` runs
with every module below it. When a source file does not parse, or a module the server imports
loads modules by name at run time (``import_module``, ``__import__``), the closure is unknown and
every package file asks. 2026-10-27..11-16: of the last 40 releases on main this rule lets 6
through (CLI habit verbs, read retries, a test fix) and holds 34.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tarfile

PACKAGE = "src/aiwiki/"
SERVER = tuple(PACKAGE + name + "/" for name in ("service", "runtime", "engine"))
# Client code that decides what the writer is sent to store, or how the audit runs: the owner's.
FREEZE = (PACKAGE + "maint/", PACKAGE + "cli/maint.py", PACKAGE + "cli/workspace.py")
AUDIT = (PACKAGE + "cli/review.py", "skills/ai-wiki-auditor/")
READER_SKILL = "skills/ai-wiki/"
# Loading a module by name hides it from the import statements the closure reads.
DYNAMIC = re.compile(r"\bimport_module\s*\(|\b__import__\s*\(|\brunpy\b|\bpkgutil\b")
STAMPS = {
    "src/aiwiki/version.py": (r'^VERSION = "[0-9][0-9A-Za-z.+-]*"$', re.M),
    "pyproject.toml": (r'(?<=\A\[project\]\nname = "ai-wiki"\n)version = "[0-9][0-9A-Za-z.+-]*"$', re.M),
    "uv.lock": (r'(?<=^name = "ai-wiki"\nversion = )"[0-9][0-9A-Za-z.+-]*"$', re.M),
}
SKILL_STAMP = r"^<!-- ai-wiki:version [0-9][0-9A-Za-z.+-]* -->$"


class GateError(Exception):
    """The repository or a commit could not be read."""


def git(repo: str, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)
    if done.returncode:
        raise GateError(f"git {' '.join(args[:2])}: {done.stderr.decode(errors='replace').strip()[:300]}")
    return done.stdout


def text_at(repo: str, sha: str, path: str) -> str | None:
    """The file at the commit, or None when the commit has no such file."""
    done = subprocess.run(["git", "-C", repo, "show", f"{sha}:{path}"], capture_output=True, check=False)
    return None if done.returncode else done.stdout.decode(errors="replace")


def sources(repo: str, sha: str) -> dict[str, str]:
    """Every Python file of the package at the commit."""
    listing = git(repo, "ls-tree", "-r", "--name-only", sha, "--", PACKAGE).decode().split("\n")
    if not any(listing):
        return {}
    archive = tarfile.open(fileobj=io.BytesIO(git(repo, "archive", sha, PACKAGE)))
    return {member.name: archive.extractfile(member).read().decode(errors="replace")
            for member in archive.getmembers() if member.isfile() and member.name.endswith(".py")}


def server_closure(files: dict[str, str]) -> set[str] | None:
    """The package files the server packages import, directly or through one another. None when
    one of them does not parse or loads modules by name: nothing can then be said to be outside it."""
    modules = {}
    for path in files:
        parts = path[len("src/"):-len(".py")].split("/")
        modules[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path

    def imported(path: str) -> set[str] | None:
        parts = path[len("src/"):-len(".py")].split("/")
        package = parts[:-1]  # a module's package, and an __init__'s own package
        try:
            tree = ast.parse(files[path])
        except (SyntaxError, ValueError):
            return None
        if DYNAMIC.search(files[path]):
            return None
        names: list[str] = [".".join(package)]  # importing a module runs each package above it first
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = package[:len(package) - node.level + 1] if node.level else []
                base = base + (node.module.split(".") if node.module else [])
                names += [".".join(base)] + [".".join([*base, alias.name]) for alias in node.names]
        found = set()
        for name in names:
            bits = name.split(".")
            # Importing a.b.c runs a and a.b too.
            found.update(modules[".".join(bits[:end])] for end in range(1, len(bits) + 1)
                         if ".".join(bits[:end]) in modules)
        return found

    seen = {path for path in files if path.startswith(SERVER)}
    todo = list(seen)
    while todo:
        more = imported(todo.pop())
        if more is None:
            return None
        todo += more - seen
        seen |= more
    return seen


def only_the_stamp_differs(path: str, before: str | None, after: str | None) -> bool:
    if before is None or after is None:
        return False
    if path in STAMPS:
        pattern, flags = STAMPS[path]
    elif path.startswith("skills/") and path.endswith("/SKILL.md"):
        pattern, flags = SKILL_STAMP, re.M
    else:
        return False
    return re.sub(pattern, "<stamp>", before, flags=flags) == re.sub(pattern, "<stamp>", after, flags=flags)


def reason(path: str, closures: tuple[set[str] | None, set[str] | None], stamp_only: bool) -> str | None:
    """Why this changed file needs the owner, or None when it is low-risk."""
    if stamp_only:
        return None
    if path in STAMPS:
        return "more than the version changed (a dependency or project setting)"
    if path.startswith(AUDIT):
        return "how the audit runs"
    if path.startswith("tests/") or path == "README.md":
        return None
    if path.startswith("docs/"):
        return "an autopilot or agent prompt" if path.startswith("docs/prompts/") else None
    if path.startswith("skills/"):
        return None if path.startswith(READER_SKILL) else "an agent's instructions (a maintainer or curator skill)"
    if path.startswith(PACKAGE) and path.endswith(".py"):
        if path.startswith(SERVER):
            return "the writer or the mirror imports it"
        if None in closures:
            return "a package file does not parse or loads modules by name, so the server's imports are unknown"
        if any(path in closure for closure in closures):
            return "the writer or the mirror imports it"
        if path.startswith(FREEZE):
            return "it freezes evidence or builds what the writer is sent"
        return None
    return "not on the low-risk list (operations, CI, packaging, prompts or an unknown path)"


def classify(repo: str, base: str, head: str) -> dict:
    base, head = (git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip() for ref in (base, head))
    changed = [path for path in git(repo, "diff", "--name-only", "--no-renames", "-z", base, head).decode().split("\0")
               if path]
    closures = (server_closure(sources(repo, base)), server_closure(sources(repo, head)))
    # A symlink or a submodule is not the file its path names: never low-risk.
    special = {line.split("\t", 1)[1] for sha in (base, head)
               for line in git(repo, "ls-tree", "-r", "-z", sha).decode().split("\0")
               if line and line.split(" ", 1)[0] in ("120000", "160000")}
    ask = []
    for path in changed:
        stamp_only = only_the_stamp_differs(path, text_at(repo, base, path), text_at(repo, head, path))
        why = "a symlink or submodule" if path in special else reason(path, closures, stamp_only)
        if why:
            ask.append({"file": path, "reason": why})
    if not changed:
        ask.append({"file": "", "reason": "the two commits have the same tree: nothing to release"})
    return {"verdict": "ask" if ask else "auto", "base": base, "head": head, "changed": len(changed), "ask": ask}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="may this release go to production without the owner's word?")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verb = verbs.add_parser("classify", help="auto (exit 0) or ask (exit 3) for the diff base..head")
    verb.add_argument("repo")
    verb.add_argument("base")
    verb.add_argument("head")
    verb.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = classify(args.repo, args.base, args.head)
    except (GateError, OSError, tarfile.TarError) as exc:
        print(f"release_gate: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"VERDICT={result['verdict']}  changed={result['changed']}  {result['base'][:7]}..{result['head'][:7]}")
        for row in result["ask"]:
            print(f"  ask: {row['file'] or '-'}: {row['reason']}")
    return 0 if result["verdict"] == "auto" else 3


if __name__ == "__main__":
    sys.exit(main())
