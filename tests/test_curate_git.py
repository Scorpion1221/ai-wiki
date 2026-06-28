"""curate._git_sync: commit always; push best-effort ('push 不了就 commit')."""
from __future__ import annotations

import subprocess
from pathlib import Path

from aiwiki.runtime import curate


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bundle"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "a.md").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_commits_even_when_push_fails(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)  # no remote configured → push will fail
    (repo / "a.md").write_text("y", encoding="utf-8")
    out = curate._git_sync(repo, "ingest: test")
    assert out["committed"] is True and out["pushed"] is False  # commit kept despite push fail
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "ingest: test" in log


def test_noop_when_nothing_changed(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    out = curate._git_sync(repo, "noop")
    assert out["committed"] is False


def test_not_a_git_repo(tmp_path: Path) -> None:
    out = curate._git_sync(tmp_path, "x")
    assert out["committed"] is False and "not a git repo" in out["note"]
