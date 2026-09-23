from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "ai-wiki-maintainer" / "scripts" / "scan_reference_repos.py"


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
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
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
        "unchanged": 1,
        "failed": 0,
        "registered_missing": 0,
        "required_missing": 0,
    }
    assert len(report["symlinks"]) == 2
    control = next(row for row in report["repos"] if "required" in row["sources"])
    assert control["baseline_required"] is True
    assert control["branch"] == "master"
    assert control["priority_counts"] == {"memory": 1, "tasks": 1}
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

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["counts"]["failed"] == 1
    assert report["counts"]["registered_missing"] == 1
    assert report["repos"][0]["state"] == "failed"


def test_scanner_uses_remote_default_develop_before_main(tmp_path: Path) -> None:
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
    assert report["repos"][0]["branch"] == "develop"
    assert report["repos"][0]["previous_branch"] == "main"
    assert report["repos"][0]["branch_changed"] is True
    assert report["repos"][0]["branch_selection"] == "remote_default"
    assert report["repos"][0]["state"] == "unchanged"
    assert report["counts"]["required_missing"] == 0


def test_scanner_follows_changed_default_branch_unless_explicitly_overridden(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, sha = make_remote(tmp_path, "preferred", {"README.md": "one\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    (work / "README.md").write_text("two\n", encoding="utf-8")
    git(work, "commit", "-am", "update develop")
    git(work, "push", "-u", "origin", "develop")
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

    checkpoint_result = scan(tmp_path, *base_args)
    assert checkpoint_result.returncode == 0, checkpoint_result.stdout + checkpoint_result.stderr
    switched = json.loads(checkpoint_result.stdout)["repos"][0]
    assert switched["branch"] == "develop"
    assert switched["previous_branch"] == "main"
    assert switched["branch_changed"] is True
    assert switched["branch_selection"] == "remote_default"
    assert switched["state"] == "changed"
    assert switched["change_count"] == 1

    override_result = scan(tmp_path, *base_args, "--branch-override", f"{remote}=main")
    assert override_result.returncode == 0, override_result.stdout + override_result.stderr
    overridden = json.loads(override_result.stdout)["repos"][0]
    assert overridden["branch"] == "main"
    assert overridden["branch_changed"] is False
    assert overridden["branch_selection"] == "override"

    missing_result = scan(tmp_path, *base_args, "--branch-override", f"{remote}=missing")
    assert missing_result.returncode == 2
    missing = json.loads(missing_result.stdout)
    assert missing["counts"]["required_missing"] == 1
    assert "explicit branch 'missing' does not exist" in missing["repos"][0]["error"]

    git(work, "push", "origin", "--delete", "main")
    stale_checkpoint_result = scan(tmp_path, *base_args)
    assert stale_checkpoint_result.returncode == 0, stale_checkpoint_result.stdout + stale_checkpoint_result.stderr
    assert json.loads(stale_checkpoint_result.stdout)["repos"][0]["branch"] == "develop"


def test_scanner_fetches_old_branch_ref_before_sha_on_default_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, remote, _ = make_remote(tmp_path, "diverged", {"README.md": "base\n"}, branch="main")
    git(work, "switch", "-c", "develop")
    (work / "README.md").write_text("develop\n", encoding="utf-8")
    git(work, "commit", "-am", "develop change")
    git(work, "push", "-u", "origin", "develop")
    git(work, "switch", "main")
    (work / "README.md").write_text("main\n", encoding="utf-8")
    git(work, "commit", "-am", "main change")
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
    )

    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["repos"][0]
    assert row["branch"] == "develop"
    assert row["branch_changed"] is True
    assert row["state"] == "changed"
    assert row["change_count"] == 1


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
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "main", "sha": previous}]}),
        encoding="utf-8",
    )

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
    args = [
        "--root", str(root), "--required-remote", str(remote),
        "--checkpoint-json", str(checkpoint), "--cache-dir", str(tmp_path / "cache"),
    ]

    unique_result = scan(tmp_path, *args)
    assert unique_result.returncode == 0, unique_result.stdout + unique_result.stderr
    row = json.loads(unique_result.stdout)["repos"][0]
    assert row["branch"] == "develop"
    assert row["branch_changed"] is True
    assert row["branch_selection"] == "remote_head_sha"

    git(work, "branch", "other")
    git(work, "push", "origin", "other")
    ambiguous_result = scan(tmp_path, *args)
    assert ambiguous_result.returncode == 2
    ambiguous = json.loads(ambiguous_result.stdout)
    assert ambiguous["counts"]["required_missing"] == 1
    assert "HEAD SHA has no unique matching branch" in ambiguous["repos"][0]["error"]
    no_checkpoint_result = scan(
        tmp_path, "--root", str(root), "--required-remote", str(remote),
        "--cache-dir", str(tmp_path / "cache"),
    )
    assert no_checkpoint_result.returncode == 2

    # Same commit on both tips has identical content; a matching checkpoint
    # retains branch identity without inventing which branch is the default.
    current = git(work, "rev-parse", "develop")
    checkpoint.write_text(
        json.dumps({"repos": [{"remote_url": str(remote), "branch": "develop", "sha": current}]}),
        encoding="utf-8",
    )
    tied_result = scan(tmp_path, *args)
    assert tied_result.returncode == 0, tied_result.stdout + tied_result.stderr
    tied = json.loads(tied_result.stdout)["repos"][0]
    assert tied["branch"] == "develop"
    assert tied["branch_selection"] == "checkpoint_head_tie"
    assert tied["branch_changed"] is False
    assert tied["state"] == "unchanged"


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
    assert ambiguous_result.returncode == 2
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
    assert fallback["branch_selection"] == "checkpoint_fallback"


def test_scanner_offline_accepts_unique_develop_branch(tmp_path: Path) -> None:
    root = tmp_path / "reference"
    root.mkdir()
    work, _, _ = make_remote(tmp_path, "offline-develop", {"README.md": "one\n"}, branch="develop")
    (root / "repo").symlink_to(work, target_is_directory=True)

    result = scan(tmp_path, "--root", str(root), "--cache-dir", str(tmp_path / "cache"), "--offline")

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["repos"][0]["branch"] == "develop"


def test_scanner_offline_origin_head_supersedes_checkpoint_branch(tmp_path: Path) -> None:
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
    row = json.loads(result.stdout)["repos"][0]
    assert row["branch"] == "develop"
    assert row["branch_changed"] is True
    assert row["branch_selection"] == "local_origin_head"


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
