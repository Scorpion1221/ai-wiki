"""scripts/deploy_bwg.sh, the production deploy (writer + read mirror on the AI Wiki host): it parses, and bad usage
is refused before the script reads a repository or reaches the host. It never runs against a host here."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_bwg.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def _run(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # ssh and scp on PATH fail loudly, so a refusal that reached the host would show.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("ssh", "scp"):
        fake = bin_dir / tool
        fake.write_text(f"#!/bin/sh\necho 'called {tool}' >&2\nexit 99\n")
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_parses_and_is_executable():
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("args", [(), ("--dry-run",), ("only-one-arg",), ("a", "b", "c")])
def test_bad_usage_is_refused_before_the_host(tmp_path, args):
    done = _run(*args, tmp_path=tmp_path)
    assert done.returncode == 2
    assert "usage:" in done.stderr
    assert "called ssh" not in done.stderr and "called scp" not in done.stderr


AUTO = SCRIPT.with_name("auto_release.sh")


def _auto(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # Every tool that could reach GitHub, the host or production fails loudly: a refusal must come first.
    bin_dir = tmp_path / "auto-bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("ssh", "scp", "gh", "ai-wiki"):
        fake = bin_dir / tool
        fake.write_text(f"#!/bin/sh\necho 'called {tool}' >&2\nexit 99\n")
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
           "AUTO_RELEASE_LOG": str(tmp_path / "auto-release.log")}
    return subprocess.run([BASH, str(AUTO), *args], capture_output=True, text=True, env=env, timeout=60)


def test_auto_release_parses_and_is_executable():
    assert subprocess.run([BASH, "-n", str(AUTO)], capture_output=True, text=True).returncode == 0
    assert AUTO.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("args", [(), ("check",), ("release", "only-the-repo"), ("verify",), ("verify", "a", "b"),
                                  ("deploy", "repo", "1"), ("check", "a", "b", "c")])
def test_auto_release_refuses_bad_usage_before_any_tool(tmp_path, args):
    done = _auto(*args, tmp_path=tmp_path)
    assert done.returncode == 2 and "auto_release.sh release" in done.stderr
    assert "called" not in done.stderr and not (tmp_path / "auto-release.log").exists()


def test_auto_release_refuses_what_is_not_a_repository_before_github_or_the_host(tmp_path):
    done = _auto("release", str(tmp_path), "94", tmp_path=tmp_path)
    assert done.returncode == 2 and "is not a git repository" in done.stderr
    assert "called" not in done.stderr and not (tmp_path / "auto-release.log").exists()


def test_auto_release_stops_when_the_pull_request_cannot_be_read(tmp_path):
    """A repository whose fetch works but whose PR cannot be read (gh fails): exit 2, nothing merged, no log line."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    for cmd in (["git", "init", "-q", "--bare", str(origin)], ["git", "init", "-q", str(repo)]):
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "f").write_text("x\n")
    for cmd in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x"],
                ["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    done = _auto("release", str(repo), "94", tmp_path=tmp_path)
    assert done.returncode == 2 and "cannot read PR 94" in done.stderr
    assert "called ssh" not in done.stderr and not (tmp_path / "auto-release.log").exists()
