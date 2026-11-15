"""scripts/release_gate.py: which diffs may reach production without the owner's word.

Each case builds a small repository shaped like this one and asks the script the way the release
loop does (``classify <repo> <base> <head>``): exit 0 is ``auto``, exit 3 ``ask`` with the file and
the reason. The last test runs the rule on this repository's own tree, so the server's real import
closure cannot drift away from what the rule assumes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_gate.py"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

sys.path.insert(0, str(SCRIPT.parent))
import release_gate  # noqa: E402

BASE = {
    "src/aiwiki/__init__.py": "",
    "src/aiwiki/version.py": 'VERSION = "0.3.33"\n',
    "src/aiwiki/service/__init__.py": "",
    "src/aiwiki/service/app.py": "from ..runtime import gate\nfrom ..maint import planner\n",
    "src/aiwiki/runtime/__init__.py": "",
    "src/aiwiki/runtime/gate.py": "from aiwiki.engine import document\n",
    "src/aiwiki/engine/__init__.py": "",
    "src/aiwiki/engine/document.py": "LIMIT = 1\n",
    "src/aiwiki/maint/__init__.py": "",
    "src/aiwiki/maint/planner.py": "FILE_TEXT_LIMIT = 24 * 1024\n",
    "src/aiwiki/maint/collect_issues.py": "from . import planner\n",
    "src/aiwiki/cli/__init__.py": "",
    "src/aiwiki/cli/main.py": "from aiwiki.cli import search\n",
    "src/aiwiki/cli/search.py": "RETRIES = 2\n",
    "src/aiwiki/cli/review.py": "DEADLINE_S = 7200\n",
    "src/aiwiki/cli/maint.py": "from aiwiki.maint import collect_issues\n",
    "src/aiwiki/cli/workspace.py": "META = '.ai-wiki'\n",
    "pyproject.toml": '[project]\nname = "ai-wiki"\nversion = "0.3.33"\ndependencies = ["pyyaml>=6"]\n',
    "uv.lock": ('[[package]]\nname = "ai-wiki"\nversion = "0.3.33"\n\n'
                '[[package]]\nname = "pyyaml"\nversion = "6.0.2"\n'),
    "skills/ai-wiki/SKILL.md": "---\nname: ai-wiki\n---\n<!-- ai-wiki:version 0.3.33 -->\nSearch first.\n",
    "skills/ai-wiki-auditor/SKILL.md": "---\nname: ai-wiki-auditor\n---\n<!-- ai-wiki:version 0.3.33 -->\nReview.\n",
    "tests/test_x.py": "def test_x():\n    assert True\n",
    "tests/fixtures/skills-fingerprint.json": '{"version": "0.3.33", "sha256": "aa"}\n',
    "docs/runbook.md": "# Runbook\n",
    "docs/prompts/production-autopilot-prompt.md": "max=100\n",
    "README.md": "# ai-wiki\n",
    "scripts/deploy_bwg.sh": "#!/usr/bin/env bash\n",
    "Dockerfile": "FROM python:3.12\n",
    ".github/workflows/ci.yml": "name: CI\n",
    "multica/ai-wiki/SKILL.md": "pointer\n",
}
BUMP = {
    "src/aiwiki/version.py": 'VERSION = "0.3.34"\n',
    "pyproject.toml": BASE["pyproject.toml"].replace("0.3.33", "0.3.34"),
    "uv.lock": BASE["uv.lock"].replace('version = "0.3.33"', 'version = "0.3.34"'),
    "skills/ai-wiki/SKILL.md": BASE["skills/ai-wiki/SKILL.md"].replace("0.3.33", "0.3.34"),
    "skills/ai-wiki-auditor/SKILL.md": BASE["skills/ai-wiki-auditor/SKILL.md"].replace("0.3.33", "0.3.34"),
    "tests/fixtures/skills-fingerprint.json": '{"version": "0.3.34", "sha256": "bb"}\n',
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root, commit(root, BASE, "base")


def gate(root: Path, base: str, head: str) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-I", str(SCRIPT), "classify", str(root), base, head, "--json"],
                          capture_output=True, text=True, check=False)
    return done.returncode, json.loads(done.stdout) if done.stdout.strip() else {"stderr": done.stderr}


def verdict(repo: tuple[Path, str], files: dict[str, str | None]) -> tuple[int, dict[str, str]]:
    root, base = repo
    code, result = gate(root, base, commit(root, files, "change"))
    assert result["verdict"] == ("auto" if code == 0 else "ask") and code in (0, 3), result
    return code, {row["file"]: row["reason"] for row in result["ask"]}


@pytest.mark.parametrize("files", [
    {"src/aiwiki/cli/search.py": "RETRIES = 3\n"},  # a module only the CLI imports
    {"src/aiwiki/cli/search.py": "RETRIES = 3\n", **BUMP},  # the same as a release: every stamp moved
    {"src/aiwiki/cli/hints.py": "HABIT = 1\n", "src/aiwiki/cli/main.py": "from aiwiki.cli import hints\n"},  # new
    {"tests/test_x.py": "def test_x():\n    assert 1\n", "tests/test_new.py": "def test_y():\n    assert 1\n"},
    {"docs/runbook.md": "# Runbook\n\nMore.\n", "README.md": "# ai-wiki\n\nMore.\n"},
    {"skills/ai-wiki/SKILL.md": BUMP["skills/ai-wiki/SKILL.md"] + "Read the hits.\n"},  # the reader's skill
])
def test_what_cannot_change_what_the_writer_stores_or_accepts_goes_by_itself(repo, files) -> None:
    assert verdict(repo, files) == (0, {})


@pytest.mark.parametrize(("files", "file", "reason"), [
    ({"src/aiwiki/service/app.py": "from ..runtime import gate\n"}, "src/aiwiki/service/app.py", "imports it"),
    ({"src/aiwiki/runtime/gate.py": "LIMIT = 9\n"}, "src/aiwiki/runtime/gate.py", "imports it"),
    ({"src/aiwiki/engine/document.py": "LIMIT = 2\n"}, "src/aiwiki/engine/document.py", "imports it"),
    # Outside the three server packages, yet the writer imports it (service/app.py does).
    ({"src/aiwiki/maint/planner.py": "FILE_TEXT_LIMIT = 64 * 1024\n"}, "src/aiwiki/maint/planner.py", "imports it"),
    ({"src/aiwiki/maint/collect_issues.py": "X = 1\n"}, "src/aiwiki/maint/collect_issues.py", "freezes evidence"),
    ({"src/aiwiki/cli/maint.py": "X = 1\n"}, "src/aiwiki/cli/maint.py", "freezes evidence"),
    ({"src/aiwiki/cli/workspace.py": "META = '.x'\n"}, "src/aiwiki/cli/workspace.py", "freezes evidence"),
    ({"src/aiwiki/cli/review.py": "DEADLINE_S = 1\n"}, "src/aiwiki/cli/review.py", "how the audit runs"),
    ({"skills/ai-wiki-auditor/SKILL.md": BUMP["skills/ai-wiki-auditor/SKILL.md"] + "Verify all.\n"},
     "skills/ai-wiki-auditor/SKILL.md", "how the audit runs"),
    ({"docs/prompts/production-autopilot-prompt.md": "max=500\n"}, "docs/prompts/production-autopilot-prompt.md",
     "prompt"),
    ({"scripts/deploy_bwg.sh": "#!/usr/bin/env bash\nrm -rf /\n"}, "scripts/deploy_bwg.sh", "low-risk list"),
    ({"Dockerfile": "FROM python:3.13\n"}, "Dockerfile", "low-risk list"),
    ({".github/workflows/ci.yml": "name: none\n"}, ".github/workflows/ci.yml", "low-risk list"),
    ({"multica/ai-wiki/SKILL.md": "other\n"}, "multica/ai-wiki/SKILL.md", "low-risk list"),
    ({"run-local.sh": "#!/bin/sh\n"}, "run-local.sh", "low-risk list"),  # a path the list never heard of
    ({"pyproject.toml": BASE["pyproject.toml"].replace("pyyaml>=6", "pyyaml>=6\", \"requests")}, "pyproject.toml",
     "more than the version"),
    ({"uv.lock": BASE["uv.lock"].replace("6.0.2", "6.0.3")}, "uv.lock", "more than the version"),
    ({"src/aiwiki/version.py": 'VERSION = "0.3.34"\nDEBUG = True\n'}, "src/aiwiki/version.py", "more than the version"),
    ({"src/aiwiki/service/app.py": None}, "src/aiwiki/service/app.py", "imports it"),  # a server module removed
])
def test_what_the_owner_decides_still_asks(repo, files, file, reason) -> None:
    code, ask = verdict(repo, files)
    assert code == 3 and reason in ask.get(file, ""), ask


def test_a_module_that_joins_or_leaves_the_server_in_this_diff_asks(repo) -> None:
    """The closure is taken at both ends: a CLI module the writer starts importing asks though it
    was low-risk at the base, and one the writer stops importing asks though it is low-risk now."""
    joins = {"src/aiwiki/cli/search.py": "RETRIES = 9\n",
             "src/aiwiki/tools.py": "from aiwiki.cli import search\n",
             "tests/test_x.py": "import aiwiki\n"}
    root, base = repo
    head = commit(root, joins, "the search module changes; a top-level module nobody imports appears")
    assert gate(root, base, head)[0] == 0  # nothing on the server imports either
    wired = commit(root, {"src/aiwiki/runtime/gate.py": "from aiwiki import tools\n"}, "the gate imports it")
    code, result = gate(root, base, wired)
    asked = {row["file"] for row in result["ask"]}
    assert code == 3 and {"src/aiwiki/cli/search.py", "src/aiwiki/tools.py", "src/aiwiki/runtime/gate.py"} <= asked
    # Leaving: at `wired` the server imports cli/search.py; a diff that unwires it and edits it still asks.
    left = commit(root, {"src/aiwiki/tools.py": "X = 1\n", "src/aiwiki/cli/search.py": "RETRIES = 0\n"}, "unwired")
    code, result = gate(root, wired, left)
    assert code == 3 and "src/aiwiki/cli/search.py" in {row["file"] for row in result["ask"]}


def test_a_file_that_does_not_parse_asks_for_every_package_file(repo) -> None:
    code, ask = verdict(repo, {"src/aiwiki/runtime/gate.py": "def broken(:\n", "src/aiwiki/cli/search.py": "R = 3\n"})
    assert code == 3 and "does not parse" in ask["src/aiwiki/cli/search.py"]


def test_no_change_and_unreadable_commits(repo) -> None:
    root, base = repo
    code, result = gate(root, base, base)
    assert code == 3 and "nothing to release" in result["ask"][0]["reason"]
    assert gate(root, base, "0" * 40)[0] == 2 and gate(root / "missing", base, base)[0] == 2
    done = subprocess.run([sys.executable, "-I", str(SCRIPT), "classify", str(root), base], capture_output=True)
    assert done.returncode == 2  # usage


def test_the_text_output_names_each_file_and_reason(repo) -> None:
    root, base = repo
    head = commit(root, {"src/aiwiki/runtime/gate.py": "LIMIT = 9\n"}, "gate")
    done = subprocess.run([sys.executable, "-I", str(SCRIPT), "classify", str(root), base, head],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 3 and done.stdout.startswith("VERDICT=ask  changed=1  ")
    assert "  ask: src/aiwiki/runtime/gate.py: the writer or the mirror imports it" in done.stdout


def test_this_repository_s_server_closure_is_what_the_rule_assumes() -> None:
    """On the real tree: the writer imports maint/planner.py (attention, inbox, triage), so a change
    there asks; nothing on the server imports the CLI; and every module of the three server packages
    is in the closure."""
    files = {str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
             for path in (ROOT / "src" / "aiwiki").rglob("*.py")}
    closure = release_gate.server_closure(files)
    assert closure is not None
    assert "src/aiwiki/maint/planner.py" in closure
    assert not [path for path in closure if path.startswith("src/aiwiki/cli/")]
    assert {path for path in files if path.startswith(release_gate.SERVER)} <= closure
    for path in ("src/aiwiki/maint/collect_issues.py", "src/aiwiki/cli/maint.py", "src/aiwiki/cli/workspace.py",
                 "src/aiwiki/cli/review.py"):
        assert path in files and path not in closure  # held by name: the rule's two lists still name real files
        assert path.startswith((*release_gate.FREEZE, *release_gate.AUDIT))
