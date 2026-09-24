from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_skills.py"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def runtime(tmp_path: Path, cli_python: str) -> dict[str, str]:
    """PATH with a system ``python3`` that lacks aiwiki and an ``ai-wiki`` CLI whose python3 runs ``cli_python``."""
    tool, bin_dir = tmp_path / "uv-tools" / "ai-wiki" / "bin", tmp_path / "bin"
    tool.mkdir(parents=True)
    bin_dir.mkdir()
    for directory, name, body in ((tool, "ai-wiki", "exit 0"), (tool, "python3", cli_python),
                                  (bin_dir, "python3", f'exec "{sys.executable}" -S "$@"')):
        (directory / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (directory / name).chmod(0o755)
    (bin_dir / "ai-wiki").symlink_to(tool / "ai-wiki")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return {**env, "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}


def test_sync_skills_apply_refuses_shims_an_old_cli_cannot_serve(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    deployed = target / "ai-wiki-maintainer" / "scripts" / "checkpoint.py"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("# the P0 script body\n", encoding="utf-8")
    # The installed CLI predates aiwiki.maint, as 0.3.0 does.
    stale = runtime(tmp_path, f'exec "{sys.executable}" -S "$@"')

    applied = run("--apply", "--dest", str(target), "ai-wiki-maintainer", env=stale)

    assert applied.returncode == 1
    assert applied.stdout.startswith(
        "ERROR ai-wiki-maintainer: not applied; checkpoint.py, issue_delta.py, scan_reference_repos.py fail in this "
        "runtime: checkpoint: fatal: cannot import aiwiki.maint.checkpoint ")
    assert applied.stdout.endswith("uv tool install --force git+https://github.com/Scorpion1221/ai-wiki && hash -r\n")
    assert deployed.read_text(encoding="utf-8") == "# the P0 script body\n"


def test_sync_skills_apply_installs_shims_the_cli_serves(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    current = runtime(tmp_path, f'PYTHONPATH="{ROOT / "src"}" exec "{sys.executable}" "$@"')

    applied = run("--apply", "--dest", str(target), "ai-wiki-maintainer", env=current)

    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert applied.stdout == f"OK ai-wiki-maintainer: {target / 'ai-wiki-maintainer'}\n"


def test_sync_skills_apply_and_check_preserves_platform_metadata(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    installed = target / "ai-wiki"
    installed.mkdir(parents=True)
    metadata = installed / "multica-metadata.json"
    metadata.write_text('{"managed": true}\n', encoding="utf-8")
    (installed / "stale-script.py").write_text("old\n", encoding="utf-8")

    applied = run("--apply", "--dest", str(target), "ai-wiki")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert metadata.read_text(encoding="utf-8") == '{"managed": true}\n'
    assert not (installed / "stale-script.py").exists()
    assert (installed / "SKILL.md").read_bytes() == (ROOT / "skills/ai-wiki/SKILL.md").read_bytes()

    checked = run("--check", "--dest", str(target), "ai-wiki")
    assert checked.returncode == 0
    assert "OK ai-wiki" in checked.stdout


def test_sync_skills_check_reports_changed_and_extra_files(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    installed = target / "ai-wiki"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("changed\n", encoding="utf-8")
    (installed / "old.txt").write_text("extra\n", encoding="utf-8")

    checked = run("--check", "--dest", str(target), "ai-wiki")
    assert checked.returncode == 1
    assert "changed SKILL.md" in checked.stdout
    assert "extra old.txt" in checked.stdout


def test_sync_skills_apply_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    target.mkdir()
    external = tmp_path / "external-ai-wiki"
    external.mkdir()
    sentinel = external / "do-not-touch.txt"
    sentinel.write_text("external\n", encoding="utf-8")
    external_metadata = external / "multica-metadata.json"
    external_metadata.write_text('{"external": true}\n', encoding="utf-8")
    installed = target / "ai-wiki"
    installed.symlink_to(external, target_is_directory=True)

    applied = run("--apply", "--dest", str(target), "ai-wiki")

    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert not installed.is_symlink()
    assert (installed / "SKILL.md").read_bytes() == (ROOT / "skills/ai-wiki/SKILL.md").read_bytes()
    assert not (installed / "multica-metadata.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "external\n"
    assert external_metadata.read_text(encoding="utf-8") == '{"external": true}\n'
