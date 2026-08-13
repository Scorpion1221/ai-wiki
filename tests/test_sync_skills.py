from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_skills.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


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
