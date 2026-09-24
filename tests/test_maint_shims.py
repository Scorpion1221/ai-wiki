"""The maintainer skill's script paths keep working as shims over ``aiwiki.maint`` (design W8).

A deployed skill runs ``python3 "$SKILL_DIR/scripts/<name>.py"`` with a system interpreter
that does not have the package; the shim re-executes with the installed CLI's interpreter.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "ai-wiki-maintainer" / "scripts"
SHIMS = {"scan_reference_repos.py": "collect_repos", "checkpoint.py": "checkpoint", "issue_delta.py": "issue_delta"}


def run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def reference_root(tmp_path: Path) -> Path:
    work, remote, root = tmp_path / "work", tmp_path / "remote.git", tmp_path / "root"
    work.mkdir()
    root.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "tasks" / "x").mkdir(parents=True)
    (work / "tasks" / "x" / "README.md").write_text("# x\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "-u", "origin", "main")
    (root / "work").symlink_to(work, target_is_directory=True)
    return root


def scan_args(tmp_path: Path, root: Path) -> list[str]:
    return ["--root", str(root), "--cache-dir", str(tmp_path / "cache"), "--priority-prefix", "tasks"]


def report(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return {**json.loads(result.stdout), "generated_at": None}


@pytest.mark.parametrize(("script", "module"), SHIMS.items())
def test_checkout_shim_runs_the_package_module(script: str, module: str) -> None:
    result = run(sys.executable, str(SCRIPTS / script), "--help")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"usage: {script} ")
    documented = importlib.import_module(f"aiwiki.maint.{module}").__doc__.split("\n", 1)[0]
    assert documented in result.stdout.replace("\n", " ")


def test_checkout_shim_output_matches_the_module(tmp_path: Path) -> None:
    root = reference_root(tmp_path)
    direct = run(sys.executable, "-m", "aiwiki.maint.collect_repos", *scan_args(tmp_path, root),
                 env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    shim = run(sys.executable, str(SCRIPTS / "scan_reference_repos.py"), *scan_args(tmp_path, root))

    assert report(shim) == report(direct)
    assert shim.stderr == direct.stderr


def deployed_skill(tmp_path: Path) -> Path:
    scripts = tmp_path / "home" / ".agents" / "skills" / "ai-wiki-maintainer" / "scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    return scripts


def cli_env(tmp_path: Path, python: str) -> dict[str, str]:
    """PATH with an ``ai-wiki`` symlinked into a uv-tool-like venv whose python3 is ``python``."""
    tool = tmp_path / "uv-tools" / "ai-wiki" / "bin"
    tool.mkdir(parents=True)
    for name, body in (("ai-wiki", "exit 0"), ("python3", python)):
        (tool / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (tool / name).chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ai-wiki").symlink_to(tool / "ai-wiki")
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "AIWIKI_MAINT_SHIM_REEXEC")}
    return {**env, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}


def test_deployed_shim_reexecs_with_the_installed_cli_interpreter(tmp_path: Path) -> None:
    root = reference_root(tmp_path)
    direct = run(sys.executable, "-m", "aiwiki.maint.collect_repos", *scan_args(tmp_path, root),
                 env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    env = cli_env(tmp_path, f'PYTHONPATH="{ROOT / "src"}" exec "{sys.executable}" "$@"')

    # -S: no site-packages, so this interpreter cannot import aiwiki (a system python3).
    shim = run(sys.executable, "-S", str(deployed_skill(tmp_path) / "scan_reference_repos.py"),
               *scan_args(tmp_path, root), env=env)

    assert report(shim) == report(direct)
    assert shim.stderr == direct.stderr


def test_deployed_shim_fails_closed_without_a_usable_cli(tmp_path: Path) -> None:
    scripts = deployed_skill(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    missing = run(sys.executable, "-S", str(scripts / "checkpoint.py"), "find", "--autopilot", "a",
                  env={**env, "PATH": f"/usr/bin{os.pathsep}/bin"})
    # The CLI's interpreter lacks the package too (an older ai-wiki): one re-exec, then stop.
    stale = run(sys.executable, "-S", str(scripts / "issue_delta.py"), "--autopilot", "a",
                env=cli_env(tmp_path, f'exec "{sys.executable}" -S "$@"'))

    for result, name in ((missing, "checkpoint"), (stale, "issue_delta")):
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr.startswith(f"{name}: fatal: cannot import aiwiki.maint.{name} ")
        assert result.stderr.rstrip().endswith("uv tool install --force git+https://github.com/Scorpion1221/ai-wiki "
                                               "&& hash -r"), "the skill preflight's own repair command"
