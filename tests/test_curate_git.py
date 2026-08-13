"""Curate git flow: clean concurrent rebases retry; real conflicts abort without LLM edits."""
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
    assert out["commit"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert out["changed_files"] == ["a.md"]
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "ingest: test" in log


def test_noop_when_nothing_changed(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    out = curate._git_sync(repo, "noop")
    assert out["committed"] is False and out["changed_files"] == []


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


def test_push_rejected_conflict_aborts_without_llm_resolution(tmp_path: Path) -> None:
    worker, remote = _new_repo_with_remote(tmp_path)
    _other_writer_pushes(remote, tmp_path, "a.md", "OTHER edit\n")  # same file → conflict
    (worker / "a.md").write_text("WORKER edit\n", encoding="utf-8")

    out = curate._commit_and_push(worker, "ingest: a.md")
    assert out["committed"] and out["pushed"] is False
    assert out["note"] == "rebase conflict; retry from remote"
    assert "<<<<<<<" not in (worker / "a.md").read_text(encoding="utf-8")


def test_commit_push_reports_durable_phase_transitions(tmp_path: Path) -> None:
    worker, _remote = _new_repo_with_remote(tmp_path)
    (worker / "b.md").write_text("job\n", encoding="utf-8")
    transitions = []

    out = curate._commit_and_push(
        worker, "ingest: durable", progress=lambda phase, result: transitions.append(
            (phase, result["commit"], result["pushed"])
        ),
    )

    assert out["committed"] is True and out["pushed"] is True
    assert transitions == [
        ("committed", out["commit"], False),
        ("pushed", out["commit"], True),
    ]


def test_commit_scope_does_not_stage_monorepo_sibling(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    bundle = repo / "kb"
    bundle.mkdir()
    (bundle / "concept.md").write_text("bundle change\n", encoding="utf-8")
    sibling = repo / "sibling.md"
    sibling.write_text("concurrent sibling\n", encoding="utf-8")

    out = curate._commit_and_push(repo, "ingest: scoped", scope=bundle)

    assert out["committed"] is True
    assert out["changed_files"] == ["kb/concept.md"]
    committed = _git(repo, "show", "--pretty=format:", "--name-only", "HEAD").stdout.splitlines()
    assert committed == ["kb/concept.md"]
    assert sibling.read_text(encoding="utf-8") == "concurrent sibling\n"
    assert "?? sibling.md" in _git(repo, "status", "--porcelain").stdout


def test_discard_untracked_symlink_does_not_touch_outside_target(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    link = repo / "escape-link"
    link.symlink_to(outside)

    curate._discard_working_tree(repo)

    assert not link.exists() and not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel"
