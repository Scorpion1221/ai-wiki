from __future__ import annotations

import hashlib
import json
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
