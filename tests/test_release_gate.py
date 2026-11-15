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

MAINTAINER_SKILL = "skills/ai-wiki-curating-maintainer/SKILL.md"
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
    MAINTAINER_SKILL: "---\nname: m\n---\n<!-- ai-wiki:version 0.3.33 -->\nCurate.\n",
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
    MAINTAINER_SKILL: BASE[MAINTAINER_SKILL].replace("0.3.33", "0.3.34"),
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
    # The maintainer's skill is that agent's instructions: what it writes into the wiki follows from it.
    ({MAINTAINER_SKILL: BUMP[MAINTAINER_SKILL] + "Skip.\n"}, MAINTAINER_SKILL, "an agent's instructions"),
    ({"skills/new-agent/SKILL.md": "---\nname: n\n---\nDo.\n"}, "skills/new-agent/SKILL.md",
     "an agent's instructions"),
    # A second line that merely looks like the version is not the package's version.
    ({"pyproject.toml": BASE["pyproject.toml"] + '\n[tool.x]\nversion = "9.9.9"\n'}, "pyproject.toml",
     "more than the version"),
    # A new module under a server package asks before any import names it.
    ({"src/aiwiki/service/extra.py": "X = 1\n"}, "src/aiwiki/service/extra.py", "imports it"),
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


COVERS = ROOT / "scripts" / "review_covers.py"


def review(reviews: Path, number: int, head: str, verdict: str = "APPROVED", findings: list | None = None) -> None:
    reviews.mkdir(exist_ok=True)
    (reviews / f"round-{number}.json").write_text(json.dumps(
        {"round": number, "verdict": verdict, "reviewedHeads": {"ai-wiki": head}, "findings": findings or []}))


def covered(reviews: Path, root: Path, head: str, repo_id: str = "ai-wiki") -> tuple[int, str]:
    done = subprocess.run([sys.executable, "-I", str(COVERS), str(reviews), str(root), repo_id, head],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def test_the_review_record_must_approve_exactly_the_code_released(repo, tmp_path) -> None:
    """The control repository's gate proves the rounds chain; this proves the round that approved is
    about the code that ships. A merge of the reviewed commit keeps its tree; a later commit does not."""
    root, base = repo
    reviews = tmp_path / "reviews"
    reviewed = commit(root, {"src/aiwiki/cli/search.py": "RETRIES = 3\n"}, "reviewed")
    assert covered(reviews, root, reviewed)[0] == 3  # no round at all
    review(reviews, 1, reviewed, "REQUEST_CHANGES")
    assert "ended REQUEST_CHANGES" in covered(reviews, root, reviewed)[1]
    review(reviews, 2, reviewed[:7])  # a short sha, as the rounds record them
    assert covered(reviews, root, reviewed) == (0, f"review_covers: round 2 approved {reviewed[:7]}, "
                                                   f"the code of {reviewed[:7]}\n")
    git(root, "checkout", "-q", "-b", "baseline", base)  # the baseline merges the issue: same tree, new commit
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "merge", "-q", "--no-ff", "-m", "merge",
        reviewed)
    merged = git(root, "rev-parse", "HEAD")
    assert merged != reviewed and covered(reviews, root, merged)[0] == 0
    later = commit(root, {"src/aiwiki/cli/search.py": "RETRIES = 99\n"}, "slipped in after the review")
    code, why = covered(reviews, root, later)
    assert code == 3 and f"reviewed {reviewed[:7]}; the release {later[:7]} holds other code" in why
    assert "names no reviewed head for other" in covered(reviews, root, merged, "other")[1]
    review(reviews, 3, reviewed, findings=[{"id": "R3-1", "severity": "P2", "status": "open"},
                                           {"id": "R3-2", "severity": "P3", "status": "open"}])
    assert "leaves R3-1 open" in covered(reviews, root, merged)[1]  # an open P2 blocks; a P3 does not
    review(reviews, 3, reviewed, findings=[{"id": "R3-2", "severity": "P3", "status": "open"}])
    assert covered(reviews, root, merged)[0] == 0
    review(reviews, 10, "0" * 40)  # round 10 is later than round 3, though it sorts before it as text
    assert covered(reviews, root, merged)[0] == 2
    (reviews / "round-11.json").write_text("{not json")
    assert covered(reviews, root, merged)[0] == 2
    assert subprocess.run([sys.executable, "-I", str(COVERS)], capture_output=True).returncode == 2


def test_a_package_init_runs_with_every_module_below_it(repo) -> None:
    """The server imports `aiwiki.maint.planner`, which runs `aiwiki/maint/__init__.py` and `aiwiki/__init__.py`
    first: a change to either reaches the writer though no import statement names it."""
    for init in ("src/aiwiki/maint/__init__.py", "src/aiwiki/__init__.py"):
        root, base = repo
        head = commit(root, {init: "import os\nos.environ.setdefault('X', '1')\n"}, f"change {init}")
        code, result = gate(root, base, head)
        assert code == 3 and {row["file"]: row["reason"] for row in result["ask"]}[init].endswith("imports it"), result
        git(root, "reset", "-q", "--hard", base)
    root, base = repo  # the CLI's own __init__ is not on the server: nothing there imports the cli package
    assert gate(root, base, commit(root, {"src/aiwiki/cli/__init__.py": "X = 1\n"}, "cli init"))[0] == 0


def test_loading_modules_by_name_makes_every_package_file_ask(repo) -> None:
    """An import by name hides its target from the closure, so nothing can be called outside the server."""
    root, base = repo
    by_name = "import importlib\nM = importlib.import_module('aiwiki.cli.search')\n"
    dynamic = commit(root, {"src/aiwiki/runtime/gate.py": by_name}, "the gate loads a module by name")
    head = commit(root, {"src/aiwiki/cli/search.py": "RETRIES = 9\n"}, "a CLI module changes")
    code, result = gate(root, dynamic, head)
    assert code == 3 and "loads modules by name" in result["ask"][0]["reason"], result


def test_a_symlink_is_never_low_risk(repo) -> None:
    root, base = repo
    (root / "tests" / "link.py").symlink_to("../src/aiwiki/runtime/gate.py")
    head = commit(root, {}, "a test that is a link into the server")
    code, result = gate(root, base, head)
    assert code == 3 and result["ask"] == [{"file": "tests/link.py", "reason": "a symlink or submodule"}]


def test_a_rename_is_judged_at_both_paths(repo) -> None:
    """Moving a server module under tests/ is a deletion from the server and an addition: the deletion asks."""
    root, base = repo
    git(root, "mv", "src/aiwiki/engine/document.py", "tests/document.py")
    head = commit(root, {}, "moved")
    code, result = gate(root, base, head)
    assert code == 3 and "src/aiwiki/engine/document.py" in {row["file"] for row in result["ask"]}
