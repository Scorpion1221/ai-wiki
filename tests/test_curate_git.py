"""curate git flow: commit always; push best-effort ('push 不了就 commit'); on a rejected
push, rebase onto the moved remote and let a claude pass resolve any conflict, then retry."""
from __future__ import annotations

import subprocess
from pathlib import Path

from aiwiki.runtime import curate


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _config(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bundle"
    repo.mkdir()
    _git(repo, "init", "-q")
    _config(repo)
    (repo / "a.md").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _new_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'remote' + a worker clone of it, both on branch main."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    _config(seed)
    (seed / "a.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    worker = tmp_path / "worker"
    subprocess.run(["git", "clone", "-q", str(remote), str(worker)], check=True)
    _config(worker)
    return worker, remote


def _other_writer_pushes(remote: Path, tmp_path: Path, path: str, content: str) -> None:
    """Simulate a different writer committing+pushing to the remote first."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _config(other)
    (other / path).write_text(content, encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", f"other: {path}")
    _git(other, "push", "-q", "origin", "main")


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


def test_push_rejected_then_clean_rebase(tmp_path: Path) -> None:
    worker, remote = _new_repo_with_remote(tmp_path)
    _other_writer_pushes(remote, tmp_path, "b.md", "from other\n")  # remote moves, different file
    (worker / "c.md").write_text("from worker\n", encoding="utf-8")  # no overlap
    out = curate._commit_and_push(worker, "ingest: c.md")
    assert out["committed"] and out["pushed"] and "conflicts_resolved" not in out
    # remote now has all three files
    log = _git(worker, "log", "--oneline").stdout
    assert "ingest: c.md" in log and "other: b.md" in log


def test_push_rejected_conflict_resolved_by_claude(tmp_path: Path, monkeypatch) -> None:
    worker, remote = _new_repo_with_remote(tmp_path)
    _other_writer_pushes(remote, tmp_path, "a.md", "OTHER edit\n")  # same file → conflict
    (worker / "a.md").write_text("WORKER edit\n", encoding="utf-8")

    # stand in for the claude conflict pass: take the union of both sides and stage it
    def fake_resolve(root: Path, files: list[str]) -> bool:
        for f in files:
            text = (root / f).read_text(encoding="utf-8")
            keep = [ln for ln in text.splitlines() if not ln.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
            (root / f).write_text("\n".join(keep) + "\n", encoding="utf-8")
            _git(root, "add", f)
        return True

    monkeypatch.setattr(curate, "_resolve_conflicts", fake_resolve)
    out = curate._commit_and_push(worker, "ingest: a.md")
    assert out["committed"] and out["pushed"] and out["conflicts_resolved"] == 1
    merged = (worker / "a.md").read_text(encoding="utf-8")
    assert "WORKER edit" in merged and "OTHER edit" in merged  # both sides kept, markers gone
    assert "<<<<<<<" not in merged


def test_push_rejected_conflict_unresolved_keeps_commit(tmp_path: Path, monkeypatch) -> None:
    worker, remote = _new_repo_with_remote(tmp_path)
    _other_writer_pushes(remote, tmp_path, "a.md", "OTHER edit\n")
    (worker / "a.md").write_text("WORKER edit\n", encoding="utf-8")
    monkeypatch.setattr(curate, "_resolve_conflicts", lambda root, files: False)  # claude can't fix it
    out = curate._commit_and_push(worker, "ingest: a.md")
    assert out["committed"] and out["pushed"] is False  # local commit survives the failed push
    assert "ingest: a.md" in _git(worker, "log", "--oneline").stdout
