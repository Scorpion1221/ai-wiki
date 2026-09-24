from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aiwiki.maint import collect_repos, planner
from aiwiki.runtime import secrets
from aiwiki.service import maint_state

ROOT = Path(__file__).resolve().parents[1]
SCANNER = (sys.executable, "-m", "aiwiki.maint.collect_repos")


def env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def make_remote(
    tmp_path: Path, name: str, files: dict[str, str], branch: str = "main"
) -> tuple[Path, Path, str]:
    work = tmp_path / f"{name}-work"
    remote = tmp_path / f"{name}.git"
    work.mkdir()
    git(work, "init", "-b", branch)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    for relative, content in files.items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "initial")
    git(tmp_path, "init", "--bare", str(remote))
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", branch)
    git(remote, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    return work, remote, git(work, "rev-parse", "HEAD")


def scan(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*SCANNER, *args],
        cwd=ROOT,
        env=env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_scanner_unions_symlinks_registry_and_required_without_writing_root(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    physical, physical_remote, physical_sha = make_remote(tmp_path, "physical", {"README.md": "one\n"})
    linked, linked_remote, _ = make_remote(tmp_path, "linked", {"src/app.py": "print(1)\n"})
    _, control_remote, _ = make_remote(
        tmp_path,
        "control",
        {"tasks/example/status.md": "done\n", "memory/learnings.md": "lesson\n"},
        branch="master",
    )
    (root / "physical").symlink_to(physical, target_is_directory=True)
    (root / "linked").symlink_to(linked, target_is_directory=True)
    sentinel = root / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"url": str(physical_remote)}, {"url": str(linked_remote)}]), encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint_value = {
        "version": 3,
        "repos": {
            "old-name": {
                "remote_url": str(physical_remote),
                "branch": "main",
                "sha": physical_sha,
            }
        },
    }
    checkpoint.write_text(
        json.dumps({"metadata": {"ai_wiki_incremental_checkpoint_v3": json.dumps(checkpoint_value)}}),
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    result = scan(
        tmp_path,
        "--root",
        str(root),
        "--registered-json",
        str(registry),
        "--checkpoint-json",
        str(checkpoint),
        "--required-remote",
        str(control_remote),
        "--branch-override",
        f"{control_remote}=master",
        "--priority-prefix",
        "tasks",
        "--priority-prefix",
        "memory",
        "--cache-dir",
        str(cache),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["counts"] == {
        "registered": 2,
        "discovered": 2,
        "required": 1,
        "unique": 3,
        "scanned": 3,
        "changed": 0,
        "new": 2,
        "rebaselined": 0,
        "unchanged": 1,
        "failed": 0,
        "carried_forward": 0,
        "truncated": 0,
        "unlisted": 0,
        "registered_missing": 0,
        "required_missing": 0,
    }
    assert len(report["symlinks"]) == 2
    control = next(row for row in report["repos"] if "required" in row["sources"])
    assert control["baseline_required"] is True
    assert control["branch"] == "master"
    assert control["priority_counts"] == {"memory": 1, "tasks": 1}
    assert control["priority_groups"] == {"memory/learnings.md": 1, "tasks/example": 1}
    assert control["path_groups"] == {"memory": 1, "tasks": 1}
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert sorted(root.iterdir()) == sorted([root / "linked", root / "physical", sentinel])


def test_scanner_reports_registered_repo_missing_in_offline_mode(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    _, remote, _ = make_remote(tmp_path, "remote-only", {"README.md": "one\n"})
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"url": str(remote)}]), encoding="utf-8")

    result = scan(
        tmp_path,
        "--root",
        str(root),
        "--registered-json",
        str(registry),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--offline",
    )

    assert result.returncode == 3
    report = json.loads(result.stdout)
    assert report["counts"]["failed"] == 1
    assert report["counts"]["registered_missing"] == 1
    assert report["repos"][0]["state"] == "failed"


def test_scanner_keeps_checkpoint_branch_and_warns_on_default_drift(tmp_path: Path) -> None:
    # Production v4 (2026-09-20) tracks crossplatformharvester@main while the remote now
    # advertises develop. The tracked release branch must not silently switch.
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "develop-default", {"README.md": "one\n"}, branch="develop")
    git(work, "branch", "main")
    git(work, "push", "origin", "main")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": sha}]}),
        encoding="utf-8",
    )

    result = scan(
        tmp_path, "--root", str(root), "--required-remote", str(remote),
        "--checkpoint-json", str(checkpoint),
        "--cache-dir", str(tmp_path / "cache"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    row = report["repos"][0]
    assert row["branch"] == "main"
    assert row["previous_branch"] == "main"
    assert row["branch_changed"] is False
    assert row["branch_selection"] == "checkpoint_continuity"
    assert row["remote_default"] == "develop"
    assert row["state"] == "unchanged"
    assert report["warnings"] == [{
        "type": "default_branch_drift", "repo": "develop-default", "remote_url": str(remote),
        "checkpoint_branch": "main", "remote_default": "develop",
    }]
    assert report["counts"]["required_missing"] == 0

    fresh = scan(tmp_path, "--root", str(root), "--required-remote", str(remote),
                 "--cache-dir", str(tmp_path / "cache"))
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    fresh_report = json.loads(fresh.stdout)
    assert fresh_report["repos"][0]["branch"] == "develop"
    assert fresh_report["repos"][0]["branch_selection"] == "remote_default"
    assert fresh_report["repos"][0]["state"] == "new"
    assert fresh_report["warnings"] == []


def test_scanner_rebaselines_only_on_override_or_missing_checkpoint_branch(tmp_path: Path) -> None:
    # Before: a default-branch change on the remote flipped main -> develop and reported
    # main..develop as an ordinary incremental delta (state=changed, no baseline).
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "preferred", {"README.md": "one\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    (work / "README.md").write_text("two\n", encoding="utf-8")
    git(work, "commit", "-am", "update develop")
    git(work, "push", "-u", "origin", "develop")
    develop_sha = git(work, "rev-parse", "HEAD")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": sha}]}),
        encoding="utf-8",
    )
    base_args = [
        "--root", str(root), "--required-remote", str(remote),
        "--checkpoint-json", str(checkpoint), "--cache-dir", str(tmp_path / "cache"),
    ]

    kept_result = scan(tmp_path, *base_args)
    assert kept_result.returncode == 0, kept_result.stdout + kept_result.stderr
    kept_report = json.loads(kept_result.stdout)
    kept = kept_report["repos"][0]
    assert kept["branch"] == "main"
    assert kept["branch_changed"] is False
    assert kept["branch_selection"] == "checkpoint_continuity"
    assert kept["state"] == "unchanged"
    assert [warning["type"] for warning in kept_report["warnings"]] == ["default_branch_drift"]

    override_result = scan(tmp_path, *base_args, "--branch-override", f"{remote}=develop")
    assert override_result.returncode == 0, override_result.stdout + override_result.stderr
    override_report = json.loads(override_result.stdout)
    switched = override_report["repos"][0]
    assert switched["branch"] == "develop"
    assert switched["branch_changed"] is True
    assert switched["branch_selection"] == "override"
    assert switched["state"] == "rebaselined"
    assert switched["rebaseline_reason"] == "branch_override"
    assert switched["baseline_required"] is True
    assert switched["merge_base"] == sha
    assert switched["change_count"] == 1
    assert switched["commit_count"] == 1
    assert override_report["counts"]["rebaselined"] == 1
    assert override_report["warnings"] == []
    assert list(override_report["checkpoint_candidate"]["repos"].values()) == [
        {"name": "preferred", "remote_url": str(remote), "branch": "develop", "sha": develop_sha}
    ]

    pinned_result = scan(tmp_path, *base_args, "--branch-override", f"{remote}=main")
    assert pinned_result.returncode == 0, pinned_result.stdout + pinned_result.stderr
    pinned_report = json.loads(pinned_result.stdout)
    assert pinned_report["repos"][0]["branch"] == "main"
    assert pinned_report["repos"][0]["branch_changed"] is False
    assert pinned_report["repos"][0]["branch_selection"] == "override"
    assert pinned_report["warnings"] == []

    missing_result = scan(tmp_path, *base_args, "--branch-override", f"{remote}=missing")
    assert missing_result.returncode == 3
    missing = json.loads(missing_result.stdout)
    assert missing["counts"]["required_missing"] == 1
    assert "explicit branch 'missing' does not exist" in missing["repos"][0]["error"]
    carried = list(missing["checkpoint_candidate"]["repos"].values())[0]
    assert (carried["branch"], carried["sha"]) == ("main", sha)
    assert carried["stale_since"] == missing["generated_at"]

    git(work, "push", "origin", "--delete", "main")
    gone_result = scan(tmp_path, *base_args)
    assert gone_result.returncode == 0, gone_result.stdout + gone_result.stderr
    gone = json.loads(gone_result.stdout)["repos"][0]
    assert gone["branch"] == "develop"
    assert gone["branch_selection"] == "remote_default"
    assert gone["state"] == "rebaselined"
    assert gone["rebaseline_reason"] == "checkpoint_branch_missing"
    assert gone["merge_base"] == sha
    assert gone["change_count"] == 1


def test_scanner_rebaseline_diffs_from_merge_base_not_across_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, base = make_remote(tmp_path, "diverged", {"README.md": "base\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    (work / "README.md").write_text("develop\n", encoding="utf-8")
    git(work, "commit", "-am", "develop change")
    git(work, "push", "-u", "origin", "develop")
    git(work, "switch", "main")
    (work / "README.md").write_text("main\n", encoding="utf-8")
    (work / "release.md").write_text("main only\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "main change")
    git(work, "push", "origin", "main")
    previous = git(work, "rev-parse", "HEAD")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": previous}]}),
        encoding="utf-8",
    )

    # Simulate a server that rejects fetch-by-SHA while still advertising old main.
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "fetch" ]; then for arg do [ "$arg" = "{previous}" ] && exit 88; done; fi\n'
        f"exec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = scan(
        tmp_path, "--root", str(root), "--required-remote", str(remote),
        "--checkpoint-json", str(checkpoint), "--cache-dir", str(tmp_path / "cache"),
        "--branch-override", f"{remote}=develop",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["repos"][0]
    assert row["branch"] == "develop"
    assert row["branch_changed"] is True
    assert row["state"] == "rebaselined"
    assert row["merge_base"] == base
    # previous..develop would also report release.md and "main change"; the fork-point diff does not.
    assert row["changes"] == [{"status": "M", "path": "README.md"}]
    assert [commit["subject"] for commit in row["commits"]] == ["develop change"]


def test_scanner_rebaselines_force_pushed_history(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, base = make_remote(tmp_path, "rewritten", {"README.md": "base\n"}, branch="main")
    (work / "README.md").write_text("old tip\n", encoding="utf-8")
    git(work, "commit", "-am", "old tip")
    git(work, "push", "origin", "main")
    previous = git(work, "rev-parse", "HEAD")
    git(work, "reset", "--hard", base)
    (work / "docs").mkdir()
    (work / "docs" / "new.md").write_text("new\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "new tip")
    git(work, "push", "--force", "origin", "main")
    git(remote, "gc", "--prune=now", "--quiet")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": previous}]}),
        encoding="utf-8",
    )

    result = scan(tmp_path, "--root", str(root), "--required-remote", str(remote),
                  "--checkpoint-json", str(checkpoint), "--cache-dir", str(tmp_path / "cache"))

    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["repos"][0]
    assert row["state"] == "rebaselined"
    assert row["rebaseline_reason"] == "history_rewritten"
    assert row["baseline_required"] is True
    assert row["branch_changed"] is False
    assert row["merge_base"] is None
    assert sorted(change["path"] for change in row["changes"]) == ["README.md", "docs/new.md"]


def test_scanner_matches_sha_only_remote_head_to_unique_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, previous = make_remote(tmp_path, "sha-only-head", {"README.md": "main\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    (work / "README.md").write_text("develop\n", encoding="utf-8")
    git(work, "commit", "-am", "develop change")
    git(work, "push", "-u", "origin", "develop")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    checkpoint = tmp_path / "checkpoint.json"

    # Model a server that advertises HEAD's SHA but omits its symref target.
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "ls-remote" ] && [ "$2" = "--symref" ]; then\n'
        "  shift 2\n"
        f'  exec {shlex.quote(real_git)} ls-remote "$@"\n'
        "fi\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    args = ["--root", str(root), "--required-remote", str(remote), "--cache-dir", str(tmp_path / "cache")]

    unique_result = scan(tmp_path, *args)
    assert unique_result.returncode == 0, unique_result.stdout + unique_result.stderr
    row = json.loads(unique_result.stdout)["repos"][0]
    assert row["branch"] == "develop"
    assert row["branch_selection"] == "remote_head_sha"
    assert row["state"] == "new"

    git(work, "branch", "other")
    git(work, "push", "origin", "other")
    ambiguous_result = scan(tmp_path, *args)
    assert ambiguous_result.returncode == 3
    ambiguous = json.loads(ambiguous_result.stdout)
    assert ambiguous["counts"]["required_missing"] == 1
    assert "HEAD SHA has no unique matching branch" in ambiguous["repos"][0]["error"]

    # A checkpoint branch that still exists needs no default: continuity resolves the tie,
    # and an unknown default is not reported as drift.
    current = git(work, "rev-parse", "develop")
    for branch, sha in (("develop", current), ("main", previous)):
        checkpoint.write_text(
            json.dumps({"repos": [{"remote_url": str(remote), "branch": branch, "sha": sha}]}),
            encoding="utf-8",
        )
        tied_result = scan(tmp_path, *args, "--checkpoint-json", str(checkpoint))
        assert tied_result.returncode == 0, tied_result.stdout + tied_result.stderr
        tied_report = json.loads(tied_result.stdout)
        tied = tied_report["repos"][0]
        assert tied["branch"] == branch
        assert tied["branch_selection"] == "checkpoint_continuity"
        assert tied["branch_changed"] is False
        assert tied["state"] == "unchanged"
        assert tied_report["warnings"] == []


def test_scanner_only_falls_back_to_unique_branch_when_remote_head_is_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, _ = make_remote(tmp_path, "headless", {"README.md": "one\n"}, branch="develop")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/nonexistent")
    args = ["--root", str(root), "--required-remote", str(remote), "--cache-dir", str(tmp_path / "cache")]

    unique_result = scan(tmp_path, *args)
    assert unique_result.returncode == 0, unique_result.stdout + unique_result.stderr
    assert json.loads(unique_result.stdout)["repos"][0]["branch"] == "develop"

    git(work, "branch", "other")
    git(work, "push", "origin", "other")
    ambiguous_result = scan(tmp_path, *args)
    assert ambiguous_result.returncode == 3
    ambiguous = json.loads(ambiguous_result.stdout)
    assert ambiguous["counts"]["failed"] == 1
    assert ambiguous["counts"]["required_missing"] == 1
    assert "default branch unavailable or ambiguous" in ambiguous["repos"][0]["error"]

    checkpoint = tmp_path / "checkpoint.json"
    current = git(work, "rev-parse", "HEAD")
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "develop", "sha": current}]}),
        encoding="utf-8",
    )
    fallback_result = scan(tmp_path, *args, "--checkpoint-json", str(checkpoint))
    assert fallback_result.returncode == 0, fallback_result.stdout + fallback_result.stderr
    fallback = json.loads(fallback_result.stdout)["repos"][0]
    assert fallback["branch"] == "develop"
    assert fallback["branch_selection"] == "checkpoint_continuity"


def test_scanner_offline_accepts_unique_develop_branch(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "offline-develop", {"README.md": "one\n"}, branch="develop")
    (root / "repo").symlink_to(work, target_is_directory=True)

    result = scan(tmp_path, "--root", str(root), "--cache-dir", str(tmp_path / "cache"), "--offline")

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["repos"][0]["branch"] == "develop"


def test_scanner_offline_keeps_checkpoint_branch_over_origin_head(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "offline-switch", {"README.md": "one\n"}, branch="main")
    (root / "repo").symlink_to(work, target_is_directory=True)
    git(work, "switch", "-c", "develop")
    git(work, "push", "-u", "origin", "develop")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    git(work, "remote", "set-head", "origin", "-a")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": sha}]}),
        encoding="utf-8",
    )

    result = scan(
        tmp_path, "--root", str(root), "--checkpoint-json", str(checkpoint),
        "--cache-dir", str(tmp_path / "cache"), "--offline",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    row = report["repos"][0]
    assert row["branch"] == "main"
    assert row["branch_changed"] is False
    assert row["branch_selection"] == "checkpoint_continuity"
    assert row["remote_default"] == "develop"
    assert [warning["remote_default"] for warning in report["warnings"]] == ["develop"]


def test_scanner_deduplicates_ssh_and_https_remote_forms(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            [
                {"url": "https://code.example.com/group/repo.git"},
                {"url": "git@code.example.com:group/repo.git"},
            ]
        ),
        encoding="utf-8",
    )

    result = scan(
        tmp_path,
        "--root",
        str(root),
        "--registered-json",
        str(registry),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--offline",
    )

    report = json.loads(result.stdout)
    assert report["counts"]["registered"] == 1
    assert report["counts"]["unique"] == 1


def test_scanner_quiet_writes_report_without_stdout(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "quiet", {"README.md": "one\n"})
    (root / "quiet").symlink_to(work, target_is_directory=True)
    output = tmp_path / "report.json"

    result = scan(
        tmp_path,
        "--root",
        str(root),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--offline",
        "--output",
        str(output),
        "--quiet",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert json.loads(output.read_text(encoding="utf-8"))["counts"]["scanned"] == 1


@pytest.mark.parametrize("output_kind", ["nested", "root", "symlink"])
def test_scanner_rejects_output_in_reference_root(tmp_path: Path, output_kind: str) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    sentinel = root / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    cache = tmp_path / "cache"
    if output_kind == "root":
        output = root
    elif output_kind == "symlink":
        alias = tmp_path / "reference-link"
        alias.symlink_to(root, target_is_directory=True)
        output = alias / "report.json"
    else:
        output = root / "nested" / "report.json"
    result = scan(tmp_path, "--root", str(root), "--cache-dir", str(cache),
                  "--output", str(output), "--offline")
    assert result.returncode == 2
    assert "--output must be outside --root" in result.stderr
    assert list(root.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert not cache.exists()


def file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

@pytest.mark.parametrize(
    ("option", "spelling"),
    [
        ("output", "child_link"),
        ("output", "real_target"),
        ("cache", "child_link"),
        ("cache", "real_target"),
    ],
    ids=["output-child-link", "output-real-target", "cache-child-link", "cache-real-target"],
)
def test_scanner_rejects_paths_inside_discovered_symlink_target_before_writes(
    tmp_path: Path, option: str, spelling: str,
) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    external, remote, _sha = make_remote(tmp_path, "external", {"README.md": "fixture\n"})
    (root / "linked-reference").symlink_to(external, target_is_directory=True)
    candidate_base = root / "linked-reference" if spelling == "child_link" else external
    candidate = candidate_base / ("report.json" if option == "output" else "scanner-cache")
    before = file_hashes(external)
    args = ["--root", str(root), "--offline"]
    if option == "output":
        args.extend(["--cache-dir", str(tmp_path / "safe-cache"), "--output", str(candidate)])
    else:
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({"repos": [{
            "remote_url": str(remote), "branch": "main", "sha": "0" * 40,
        }]}), encoding="utf-8")
        args.extend([
            "--checkpoint-json", str(checkpoint),
            "--cache-dir", str(candidate),
            "--output", str(tmp_path / "safe-report.json"),
        ])
    result = scan(tmp_path, *args)
    assert result.returncode == 2
    expected_option = "--output" if option == "output" else "--cache-dir"
    assert f"{expected_option} must be outside discovered reference repositories" in result.stderr
    assert not candidate.exists()
    after = file_hashes(external)
    assert after == before


def test_scanner_carries_failed_repo_rows_forward_until_it_recovers(tmp_path: Path) -> None:
    # 09-22 production: one unreachable repo emptied its checkpoint row, so the whole
    # checkpoint had to stay frozen. A failed repo now keeps its last good row.
    root = tmp_path / "reference"
    root.mkdir()
    work, healthy_remote, healthy_sha = make_remote(tmp_path, "healthy", {"README.md": "one\n"})
    _, flaky_remote, flaky_sha = make_remote(tmp_path, "flaky", {"README.md": "one\n"})
    flaky_url = flaky_remote.as_uri()
    (work / "README.md").write_text("two\n", encoding="utf-8")
    git(work, "commit", "-am", "advance healthy")
    git(work, "push", "origin", "main")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 4, "issues": {"updated_at": "2026-09-19T16:40:04Z", "id": "x"},
                                      "repos": {"a": {"remote_url": str(healthy_remote), "branch": "main",
                                                      "sha": healthy_sha},
                                                "b": {"remote_url": flaky_url, "branch": "main",
                                                      "sha": flaky_sha}}}), encoding="utf-8")
    args = ["--root", str(root), "--required-remote", str(healthy_remote), "--required-remote", flaky_url,
            "--cache-dir", str(tmp_path / "cache")]
    parked = tmp_path / "parked.git"
    flaky_remote.rename(parked)

    first = scan(tmp_path, *args, "--checkpoint-json", str(checkpoint))
    assert first.returncode == 3, first.stdout + first.stderr
    report = json.loads(first.stdout)
    assert report["counts"]["failed"] == 1
    assert report["counts"]["carried_forward"] == 1
    candidate = report["checkpoint_candidate"]
    assert candidate["issues"] == {"updated_at": "2026-09-19T16:40:04Z", "id": "x"}
    rows = {row["name"]: row for row in candidate["repos"].values()}
    assert rows["healthy"]["sha"] == git(work, "rev-parse", "HEAD")
    assert "stale_since" not in rows["healthy"]
    assert rows["flaky"]["sha"] == flaky_sha
    assert rows["flaky"]["branch"] == "main"
    assert rows["flaky"]["stale_since"] == report["generated_at"]
    assert rows["flaky"]["last_error"]
    failed = next(row for row in report["repos"] if row["state"] == "failed")
    assert failed["stale_since"] == report["generated_at"]

    # The carried row is itself a valid checkpoint input: stale_since survives repeated failures.
    carried = tmp_path / "carried.json"
    carried.write_text(json.dumps(candidate), encoding="utf-8")
    second = scan(tmp_path, *args, "--checkpoint-json", str(carried))
    assert second.returncode == 3
    again = {row["name"]: row for row in json.loads(second.stdout)["checkpoint_candidate"]["repos"].values()}
    assert again["flaky"]["stale_since"] == rows["flaky"]["stale_since"]

    parked.rename(flaky_remote)
    recovered = scan(tmp_path, *args, "--checkpoint-json", str(carried))
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    healed = {row["name"]: row for row in json.loads(recovered.stdout)["checkpoint_candidate"]["repos"].values()}
    assert healed["flaky"] == {"name": "flaky", "remote_url": flaky_url, "branch": "main", "sha": flaky_sha}


def test_scanner_reports_invalid_urls_as_failed_rows(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    _, remote, _ = make_remote(tmp_path, "valid", {"README.md": "one\n"})
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"url": "/nonexistent/repo"}, {"url": "not a url"}, {"url": str(remote)}]),
                        encoding="utf-8")

    result = scan(tmp_path, "--root", str(root), "--registered-json", str(registry),
                  "--required-remote", "relative/missing", "--cache-dir", str(tmp_path / "cache"))

    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["counts"]["registered"] == 3
    assert report["counts"]["registered_missing"] == 2
    assert report["counts"]["required_missing"] == 1
    assert report["counts"]["scanned"] == 1
    assert report["counts"]["failed"] == 3
    assert all("no host/path" in row["error"] for row in report["repos"] if row["state"] == "failed")


def test_scanner_bounds_git_calls_and_never_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    _, slow_remote, _ = make_remote(tmp_path, "slow", {"README.md": "one\n"})
    _, fast_remote, _ = make_remote(tmp_path, "fast", {"README.md": "one\n"})
    secret_url = "https://robot:s3cr3t-token@code.example.com/group/private.git"
    real_git = shutil.which("git")
    assert real_git is not None
    env_log = tmp_path / "git-env.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'echo "prompt=$GIT_TERMINAL_PROMPT askpass=$GIT_ASKPASS" >> {shlex.quote(str(env_log))}\n'
        'if [ "$1" = "ls-remote" ]; then for arg do\n'
        f'  [ "$arg" = {shlex.quote(str(slow_remote))} ] && exec sleep 30\n'
        '  case "$arg" in https://*)\n'
        '    echo "fatal: unable to access \'$arg\': Could not resolve host" >&2; exit 128;;\n'
        "  esac\n"
        "done; fi\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = subprocess.run(
        [*SCANNER, "--root", str(root), "--required-remote", str(slow_remote),
         "--required-remote", str(fast_remote), "--required-remote", secret_url,
         "--cache-dir", str(tmp_path / "cache"), "--git-timeout", "2"],
        cwd=ROOT, env=env(), capture_output=True, text=True, check=False, timeout=25,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "s3cr3t" not in result.stdout + result.stderr
    rows = {row["name"]: row for row in json.loads(result.stdout)["repos"]}
    assert rows["slow"]["error"] == "git ls-remote timed out after 2s"
    assert rows["private"]["state"] == "failed"
    assert "***@code.example.com" in rows["private"]["error"]
    assert rows["fast"]["state"] == "new"
    calls = env_log.read_text(encoding="utf-8").splitlines()
    assert calls and all(line.startswith("prompt=0 askpass=/") for line in calls)


def test_scanner_unreadable_checkpoint_is_fatal_without_report(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text("{not json", encoding="utf-8")
    output = tmp_path / "report.json"

    result = scan(tmp_path, "--root", str(root), "--checkpoint-json", str(checkpoint),
                  "--cache-dir", str(tmp_path / "cache"), "--output", str(output))

    assert result.returncode == 2
    assert "scan_reference_repos: fatal:" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_scanner_aggregates_all_paths_and_flags_truncation_loudly(tmp_path: Path) -> None:
    # Production scans capped changes at 200 paths with only a boolean flag, so a large
    # context-repo delta silently dropped most of its task roots.
    root = tmp_path / "reference"
    root.mkdir()
    files = {"README.md": "root\n", "src/a.py": "a\n", "src/b.py": "b\n", "src/c.py": "c\n"}
    files.update({f"tasks/2026092{day}-topic/status.md": "done\n" for day in range(3)})
    files["tasks/20260920-topic/prd.md"] = "prd\n"
    _, remote, _ = make_remote(tmp_path, "control", files)

    result = scan(tmp_path, "--root", str(root), "--required-remote", str(remote),
                  "--priority-prefix", "tasks", "--cache-dir", str(tmp_path / "cache"),
                  "--max-paths", "2", "--max-commits", "1")

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    row = report["repos"][0]
    assert row["change_count"] == 8
    assert len(row["changes"]) == 2
    assert row["paths_truncated"] is True
    assert row["path_groups"] == {".": 1, "src": 3, "tasks": 4}
    assert row["priority_groups"] == {
        "tasks/20260920-topic": 2, "tasks/20260921-topic": 1, "tasks/20260922-topic": 1,
    }
    assert row["priority_truncated"] is True
    assert row["commits_truncated"] is False
    assert report["counts"]["truncated"] == 1
    assert report["warnings"] == [{
        "type": "truncated", "repo": "control", "remote_url": str(remote),
        "fields": ["changes", "priority_changes"], "change_count": 8, "commit_count": 1,
        "priority_count": 4, "max_paths": 2, "max_commits": 1,
    }]


def test_scanner_carries_unlisted_checkpoint_repos_as_stale_partial_rows(tmp_path: Path) -> None:
    # 13 of the 24 live v4 repos are only discovered below the reference root. When the
    # root lost them (WAIO-89/95: runtime without the Linux path), the scan still exited 0
    # and their rows never aged. They are now partial (exit 3) and carry stale_since.
    root = tmp_path / "reference"
    root.mkdir()
    _, listed_remote, listed_sha = make_remote(tmp_path, "listed", {"README.md": "one\n"})
    old_row = {"remote_url": "https://code.example.com/group/retired.git", "branch": "master",
               "sha": "a" * 40, "stale_since": "2026-09-20T00:00:00Z", "last_error": "unreachable"}
    fresh_row = {"remote_url": "https://code.example.com/group/root-only.git", "branch": "main", "sha": "b" * 40}
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 3, "repos": {
        "retired": old_row,
        "root-only": fresh_row,
        "listed": {"remote_url": str(listed_remote), "branch": "main", "sha": listed_sha},
    }}), encoding="utf-8")

    result = scan(tmp_path, "--root", str(root), "--required-remote", str(listed_remote),
                  "--checkpoint-json", str(checkpoint), "--cache-dir", str(tmp_path / "cache"))

    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["counts"]["unlisted"] == 2
    assert report["counts"]["failed"] == 0
    assert report["warnings"] == [
        {"type": "checkpoint_repo_unlisted", "repo": "retired",
         "remote_url": "https://code.example.com/group/retired.git"},
        {"type": "checkpoint_repo_unlisted", "repo": "root-only",
         "remote_url": "https://code.example.com/group/root-only.git"},
    ]
    rows = {row["name"]: row for row in report["checkpoint_candidate"]["repos"].values()}
    unlisted_error = "not in inventory (not discovered/registered/required)"
    assert rows["retired"] == {**old_row, "name": "retired", "last_error": unlisted_error}
    assert rows["root-only"] == {**fresh_row, "name": "root-only", "stale_since": report["generated_at"],
                                 "last_error": unlisted_error}
    assert rows["listed"]["sha"] == listed_sha


def test_scanner_missing_root_is_fatal_without_report(tmp_path: Path) -> None:
    output = tmp_path / "report.json"

    result = scan(tmp_path, "--root", str(tmp_path / "unmounted"), "--cache-dir", str(tmp_path / "cache"),
                  "--output", str(output))

    assert result.returncode == 2
    assert "is not a directory" in result.stderr
    assert not output.exists()


def test_scanner_reads_checkpoint_find_output_and_encoded_metadata(tmp_path: Path) -> None:
    # `checkpoint.py find` wraps the checkpoint as {found, checkpoint}; `metadata get` returns
    # the stored JSON string encoded once more. Both used to read as "no checkpoint": every
    # repo became new, drifting repos flipped to their dev default, and nothing warned.
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "release", {"README.md": "one\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    git(work, "push", "-u", "origin", "develop")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    checkpoint = {"version": 4, "repos": {"r": {"name": "release", "remote_url": str(remote),
                                                "branch": "main", "sha": sha}},
                  "issues": {"updated_at": "2026-09-19T16:40:04Z", "id": "x"}}
    stored = json.dumps(checkpoint)
    inputs = {
        "find": {"found": True, "version": 4, "checkpoint": checkpoint},
        "metadata-get": json.dumps(stored),
        "metadata-list": {"ai_wiki_incremental_checkpoint_v4": stored},
    }
    for label, value in inputs.items():
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(value), encoding="utf-8")

        result = scan(tmp_path, "--root", str(root), "--required-remote", str(remote),
                      "--checkpoint-json", str(path), "--cache-dir", str(tmp_path / "cache"))

        assert result.returncode == 0, label + result.stdout + result.stderr
        report = json.loads(result.stdout)
        row = report["repos"][0]
        assert (row["state"], row["branch"], row["previous_sha"]) == ("unchanged", "main", sha), label
        assert [warning["type"] for warning in report["warnings"]] == ["default_branch_drift"], label
        assert report["checkpoint_candidate"]["issues"] == checkpoint["issues"], label

    not_found = tmp_path / "not-found.json"
    not_found.write_text(json.dumps({"found": False, "valid": {"v4": 0, "v3": 0}}), encoding="utf-8")
    output = tmp_path / "report.json"
    refused = scan(tmp_path, "--root", str(root), "--required-remote", str(remote),
                   "--checkpoint-json", str(not_found), "--cache-dir", str(tmp_path / "cache"), "--output", str(output))
    assert refused.returncode == 2
    assert "has no usable repos rows" in refused.stderr
    assert not output.exists()


def test_scanner_keeps_non_ascii_paths_verbatim(tmp_path: Path) -> None:
    # git's default core.quotePath turned tasks/<中文 root> into '"tasks/\\344...' so the
    # tasks priority prefix and its task-root groups missed it entirely.
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "context", {"tasks/20260920-中文需求/prd.md": "prd\n",
                                                           "README.md": "one\n"})
    args = ["--root", str(root), "--required-remote", str(remote), "--priority-prefix", "tasks",
            "--cache-dir", str(tmp_path / "cache")]

    fresh = scan(tmp_path, *args)

    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    row = json.loads(fresh.stdout)["repos"][0]
    assert row["path_groups"] == {".": 1, "tasks": 1}
    assert row["priority_groups"] == {"tasks/20260920-中文需求": 1}
    assert {change["path"] for change in row["changes"]} == {"README.md", "tasks/20260920-中文需求/prd.md"}

    git(work, "mv", "tasks/20260920-中文需求/prd.md", "tasks/20260920-中文需求/需求.md")
    (work / "tasks" / "20260921-新任务").mkdir()
    (work / "tasks" / "20260921-新任务" / "状态 notes.md").write_text("done\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "rename and add")
    git(work, "push", "origin", "main")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": sha}]}),
                          encoding="utf-8")

    changed = scan(tmp_path, *args, "--checkpoint-json", str(checkpoint))

    assert changed.returncode == 0, changed.stdout + changed.stderr
    row = json.loads(changed.stdout)["repos"][0]
    assert row["state"] == "changed"
    assert sorted(row["changes"], key=lambda change: change["path"]) == [
        {"status": "R100", "old_path": "tasks/20260920-中文需求/prd.md", "path": "tasks/20260920-中文需求/需求.md"},
        {"status": "A", "path": "tasks/20260921-新任务/状态 notes.md"},
    ]
    assert row["priority_groups"] == {"tasks/20260920-中文需求": 1, "tasks/20260921-新任务": 1}


# Repos collector (design §4.3): report -> planner candidates with frozen evidence -> cursor.


def scan_report(tmp_path: Path, root: Path, *args: str) -> dict:
    result = scan(tmp_path, "--root", str(root), "--cache-dir", str(tmp_path / "cache"), *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def commit_all(work: Path, message: str, files: dict[str, str | bytes | None]) -> str:
    for relative, content in files.items():
        target = work / relative
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", message)
    git(work, "push")
    return git(work, "rev-parse", "HEAD")


def cursor_file(tmp_path: Path, cursor: dict) -> str:
    path = tmp_path / "cursor-checkpoint.json"
    path.write_text(json.dumps(collect_repos.checkpoint_from_cursor(cursor)), encoding="utf-8")
    return str(path)


def test_collect_freezes_task_roots_and_top_directories_and_counts_noise(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, first = make_remote(tmp_path, "control", {
        "README.md": "control\n", "tasks/h5/README.md": "# h5\n", "tasks/h5/sql/q.sql": "select 1;\n",
        "memory/learnings.md": "- one\n", "src/app.ts": "export {}\n", "package-lock.json": "{}\n",
        "tests/test_app.py": "def test(): pass\n",
    }, branch="master")
    (root / "control").symlink_to(work, target_is_directory=True)
    identity = collect_repos.canonical_remote(str(remote))

    baseline = collect_repos.collect(scan_report(tmp_path, root, "--priority-prefix", "tasks"))

    [summary] = baseline["candidates"]
    assert summary["topic_key"] == f"rebaseline:{identity}"
    assert summary["origin"]["reason"] == "new"
    assert "- tasks/h5: 2" in summary["files"][0]["data"].decode()
    assert baseline["cursor"] == {identity: {"branch": "master", "sha": first, "stale_since": None, "error": None}}

    secret = "sk-" + "a" * 32
    head = commit_all(work, "second", {
        "README.md": None,
        "tasks/h5/status.md": "status: done\n",
        "tasks/h5/sql/q.sql": "select 2;\n",
        "memory/learnings.md": "- one\n- two\n",
        "src/app.ts": f"const client = new Client('{secret}')\n",
        "package-lock.json": '{"lockfileVersion": 3}\n',
        "tests/test_app.py": "def test(): assert True\n",
        "assets/logo.png": b"\x89PNG\x00\x01",
        "docs/big.md": "x" * 30000 + "\n",
        "docs/blob.dat": b"\x00\x01binary",
    })
    # --max-paths 1 truncates the report's lists; the collector re-reads the full change set.
    report = scan_report(tmp_path, root, "--priority-prefix", "tasks", "--max-paths", "1",
                         "--checkpoint-json", cursor_file(tmp_path, baseline["cursor"]))
    row = report["repos"][0]
    assert (row["state"], row["paths_truncated"]) == ("changed", True)

    collected = collect_repos.collect(report)

    by_topic = {candidate["topic_key"].removeprefix(f"repo:{identity}#"): candidate
                for candidate in collected["candidates"]}
    assert sorted(by_topic) == [".", "docs", "memory/learnings.md", "src", "tasks/h5"]
    assert collected["counts"] == {"candidates": 5, "changed": 1, "excluded": 0, "failed": 0, "rebaselined": 0,
                                   "noise": {"binary": 1, "lockfile": 1, "test": 1}}
    assert collected["cursor"] == {identity: {"branch": "master", "sha": head, "stale_since": None, "error": None}}

    tasks = by_topic["tasks/h5"]
    assert [file["name"] for file in tasks["files"]] == [
        f"changes-{first[:7]}-{head[:7]}.md", collect_repos.snapshot_name("tasks/h5/status.md"),
        collect_repos.snapshot_name("tasks/h5/sql/q.sql")]
    assert tasks["files"][1]["name"].endswith("-status.md")
    status = tasks["files"][1]
    assert status["data"] == b"status: done\n"
    assert status["sha256"] == hashlib.sha256(b"status: done\n").hexdigest()
    assert status["origin"] == {
        "kind": "git-file", "remote": row["remote_url"], "commit": head, "path": "tasks/h5/status.md",
        "blob": git(work, "rev-parse", f"{head}:tasks/h5/status.md"), "truncated": False, "redactions": 0,
    }
    manifest = tasks["files"][0]["data"].decode()
    assert "- A tasks/h5/status.md" in manifest and "- M tasks/h5/sql/q.sql" in manifest
    assert "second" in manifest and "- commits: 1" in manifest
    assert tasks["origin"] == {"kind": "repo", "remote": row["remote_url"], "commit": head, "branch": "master",
                               "base": first}
    assert tasks["brief"] == f"control tasks/h5: 1 commits since {first[:7]}; changed sql/q.sql, status.md"

    app = by_topic["src"]["files"][1]
    assert secret not in app["data"].decode() and app["origin"]["redactions"] == 1
    big = by_topic["docs"]["files"][1]
    assert big["origin"]["path"] == "docs/big.md"
    assert big["origin"]["truncated"] and big["bytes"] <= planner.FILE_TEXT_LIMIT
    assert by_topic["docs"]["brief"].endswith("; 1 not frozen")  # docs/blob.dat is binary content
    top = by_topic["."]
    assert [file["name"] for file in top["files"]] == [f"changes-{first[:7]}-{head[:7]}.md"]
    assert "- D README.md" in top["files"][0]["data"].decode()
    assert "noise (counted, not frozen): lockfile 1" in top["files"][0]["data"].decode()
    assert all(sum(file["bytes"] for file in candidate["files"]) <= planner.ITEM_TEXT_LIMIT
               for candidate in collected["candidates"])

    items = planner.plan(collected["candidates"])
    assert [(item["topic_key"].removeprefix(f"repo:{identity}#"), item["priority"]) for item in items] == [
        ("memory/learnings.md", 80), ("tasks/h5", 70), (".", 40), ("docs", 40), ("src", 40),
    ]
    # Collecting the same delta again yields the same item keys, which the queue dedupes on.
    again = planner.plan(collect_repos.collect(report)["candidates"])
    assert list(map(_key, again)) == list(map(_key, items))
    excluded = collect_repos.collect(report, exclude_remotes=[str(remote)])  # the bundle's own repository
    assert (excluded["candidates"], excluded["counts"]["excluded"]) == ([], 1)


def test_consecutive_ranges_merge_by_path_without_losing_evidence(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "control", {"tasks/a/README.md": "a\n"})
    (root / "control").symlink_to(work, target_is_directory=True)
    cursor = collect_repos.collect(scan_report(tmp_path, root))["cursor"]
    first = commit_all(work, "one", {"tasks/a/README.md": "top v1\n", "tasks/a/sub/README.md": "sub v1\n",
                                     "tasks/a/status.md": "doing\n", "tasks/a/需求 说明.md": "需求\n"})
    one = collect_repos.collect(scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, cursor)))
    second = commit_all(work, "two", {"tasks/a/sub/README.md": "sub v2\n", "tasks/a/status.md": "done\n"})
    two = collect_repos.collect(scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, one["cursor"])))

    [[run1], [run2]] = [[candidate["files"] for candidate in result["candidates"]] for result in (one, two)]
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", file["name"]) for file in run1 + run2)
    # The queue folds a same-topic collection into a ready item by file name, newest wins.
    merged = {file["name"]: file for file in run1} | {file["name"]: file for file in run2}
    snapshots = {file["origin"]["path"]: file["data"].decode() for file in merged.values() if "path" in file["origin"]}
    assert snapshots == {"tasks/a/README.md": "top v1\n", "tasks/a/sub/README.md": "sub v2\n",
                         "tasks/a/status.md": "done\n", "tasks/a/需求 说明.md": "需求\n"}
    manifests = [file for file in merged.values() if file["origin"]["kind"] == "git-changes"]
    assert sorted(file["origin"]["commit"] for file in manifests) == sorted([first, second])


def _key(item: dict) -> str:
    """The writer's key of a planned item (POST /maint/items dedupes on it)."""
    return maint_state.item_key(item["origin"]["kind"], item["topic_key"], [file["sha256"] for file in item["files"]])


def test_collected_items_fit_the_writer_queue_under_one_key(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "control", {"tasks/a/README.md": "a\n"})
    (root / "control").symlink_to(work, target_is_directory=True)
    cursor = collect_repos.collect(scan_report(tmp_path, root))["cursor"]
    commit_all(work, "many", {f"tasks/a/notes/n{i:03}.md": f"note {i}\n" for i in range(80)})
    report = scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, cursor))

    [candidate] = collect_repos.collect(report)["candidates"]
    assert len(candidate["files"]) == planner.ITEM_FILE_LIMIT == maint_state.MAX_FILES
    assert candidate["brief"].endswith(f"; {80 - planner.ITEM_FILE_LIMIT + 1} not frozen")

    # W9's POST /maint/items body: the writer accepts the batch and keys it by its evidence.
    [item] = planner.plan([candidate])
    body = [{"origin": item["origin"], "topic_key": item["topic_key"], "priority": item["priority"],
             "brief": item["brief"], "files": [{"name": f["name"], "origin": f["origin"],
                                                "content_b64": base64.b64encode(f["data"]).decode()}
                                               for f in item["files"]]}]
    bundle = tmp_path / "kb"
    [queued] = maint_state.enqueue(bundle, body, principal="process:test")["items"]
    assert (queued["result"], queued["item_key"]) == ("created", _key(item))
    assert maint_state.enqueue(bundle, body, principal="process:test")["duplicate"] == 1
    first = item["files"][1]
    assert maint_state.read_file(bundle, queued["id"], first["name"]) == first["data"]


def test_collect_never_freezes_credentials(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "ops", {"docs/setup.md": "setup\n"})
    (root / "ops").symlink_to(work, target_is_directory=True)
    cursor = collect_repos.collect(scan_report(tmp_path, root))["cursor"]
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
    commit_all(work, "deploy", {
        "deploy/id_ed25519": pem,
        "config/.env.production": "DB_PASS=hunter2\n",
        "docs/setup.md": "Use aiw_c_9f8e7d6c5b4a39281706f5e4 with AKIAIOSFODNN7EXAMPLE.\n" + pem,
    })

    collected = collect_repos.collect(scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, cursor)))

    assert collected["counts"]["noise"] == {"secret": 2}
    assert sorted(candidate["topic_key"].rsplit("#", 1)[1] for candidate in collected["candidates"]) == ["docs"]
    frozen = [file for candidate in collected["candidates"] for file in candidate["files"]]
    assert [file["origin"].get("path") for file in frozen] == [None, "docs/setup.md"]
    assert frozen[1]["origin"]["redactions"] == 3
    for file in frozen:
        text = file["data"].decode()
        assert secrets.scan(text) == [] and "hunter2" not in text


def test_collect_keeps_the_previous_cursor_row_when_evidence_cannot_be_read(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, first = make_remote(tmp_path, "flaky", {"docs/a.md": "a\n"})
    (root / "flaky").symlink_to(work, target_is_directory=True)
    identity = collect_repos.canonical_remote(str(remote))
    cursor = collect_repos.collect(scan_report(tmp_path, root))["cursor"]
    commit_all(work, "second", {"docs/a.md": "b\n"})
    report = scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, cursor))
    report["repos"][0]["object_repo"] = str(tmp_path / "gone")

    collected = collect_repos.collect(report)

    assert collected["candidates"] == []
    assert collected["counts"]["failed"] == 1 and collected["failed"][0]["error"]
    row = collected["cursor"][identity]
    assert (row["branch"], row["sha"], row["stale_since"]) == ("main", first, report["generated_at"])
    assert row["error"] == collected["failed"][0]["error"]
    # Failing again keeps the first failure's age, like the scanner's carried rows.
    report = scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, collected["cursor"]))
    report["repos"][0]["object_repo"] = str(tmp_path / "gone")
    report["generated_at"] = "2099-01-01T00:00:00Z"
    again = collect_repos.collect(report, previous=collected["cursor"])["cursor"][identity]
    assert (again["sha"], again["stale_since"]) == (first, row["stale_since"])


def test_collect_summarizes_rewritten_history_instead_of_diffing_across_lineages(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, _ = make_remote(tmp_path, "rewritten", {"docs/a.md": "a\n", "src/app.ts": "1\n"})
    (root / "rewritten").symlink_to(work, target_is_directory=True)
    cursor = collect_repos.collect(scan_report(tmp_path, root))["cursor"]
    (work / "docs" / "a.md").write_text("rewritten\n", encoding="utf-8")
    git(work, "commit", "-a", "--amend", "-m", "rewritten")
    git(work, "push", "--force")

    report = scan_report(tmp_path, root, "--checkpoint-json", cursor_file(tmp_path, cursor))
    collected = collect_repos.collect(report)

    [summary] = collected["candidates"]
    assert summary["topic_key"] == f"rebaseline:{collect_repos.canonical_remote(str(remote))}"
    assert summary["origin"]["reason"] == "history_rewritten"
    text = summary["files"][0]["data"].decode()
    assert "Not an incremental diff" in text and "- docs: 1" in text and "- src: 1" in text
    assert planner.plan([summary])[0]["priority"] == 40
    assert collected["counts"] == {"candidates": 1, "changed": 0, "excluded": 0, "failed": 0, "rebaselined": 1,
                                   "noise": {}}


def test_repos_cursor_round_trips_through_the_scanner_checkpoint_form() -> None:
    cursor = {
        "code.example.com/web/control": {"branch": "master", "sha": "a" * 40, "stale_since": None, "error": None},
        "code.example.com/web/stale": {"branch": "main", "sha": "b" * 40, "stale_since": "2026-10-16T20:00:00Z",
                                       "error": "git ls-remote timed out after 120s"},
    }

    checkpoint = collect_repos.checkpoint_from_cursor(cursor)

    by_remote, _ = collect_repos.checkpoint_repositories(checkpoint)
    assert sorted(by_remote) == sorted(cursor)
    assert checkpoint["repos"][collect_repos.repo_id("code.example.com/web/stale")] == {
        "name": "stale", "remote_url": "https://code.example.com/web/stale", "branch": "main", "sha": "b" * 40,
        "stale_since": "2026-10-16T20:00:00Z", "last_error": "git ls-remote timed out after 120s",
    }
    assert collect_repos.cursor_from_report({"checkpoint_candidate": checkpoint}) == cursor
