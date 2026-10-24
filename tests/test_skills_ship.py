"""The agent skills ship with the CLI: stamped with its version, installed by it, updated with it."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from aiwiki import skills
from aiwiki.cli import main as cli
from aiwiki.version import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_every_skill_is_stamped_with_this_version() -> None:
    for name in skills.NAMES:
        text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert skills.stamp(text) == VERSION, f"{name}: bump its <!-- ai-wiki:version --> stamp with the package"
        assert skills.skill_name(text) == name


def test_the_skill_list_is_the_repository_list() -> None:
    assert sorted(skills.NAMES) == sorted(p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())


def test_every_skill_updates_before_a_run_and_keeps_tokens_out_of_argv() -> None:
    # Owner decision 2026-10-24: agents update the CLI themselves, shared Multica host included.
    for name in skills.NAMES:
        text = " ".join((ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split())
        assert "`ai-wiki update --check`" in text, f"{name}: its preflight must run `ai-wiki update --check`"
        assert "never reinstall" not in text.lower(), f"{name}: contradicts the self-update rule"
        assert "--token" not in text, f"{name}: a token goes in $AIWIKI_TOKEN, never in a command"
        if name != "okf-knowledge-curator":   # it ships with ai-wiki; its callers' preflights update it
            assert "`ai-wiki update && hash -r`" in text and "Never update " in text, \
                f"{name}: update, rehash and re-read before the run, never during its lease"
    # The live agents' Multica instructions carry the same rule; they must not contradict their Skill.
    for prompt in ("production", "shadow", "auditor"):
        text = " ".join((ROOT / "docs" / "prompts" / f"{prompt}-agent-instructions.md").read_text("utf-8").split())
        assert "`ai-wiki update --check`" in text and "`ai-wiki update && hash -r`" in text, prompt
        assert "Never update between " in text and "never reinstall" not in text.lower(), prompt


def test_install_status_and_refusals(tmp_path: Path, capsys) -> None:
    dest = tmp_path / "skills"
    assert {row["status"] for row in skills.status(dest)} == {"missing"}

    rows = skills.install(dest)
    assert {row["status"] for row in rows} == {"installed"}
    assert {row["status"] for row in skills.status(dest)} == {"current"}
    assert {row["status"] for row in skills.install(dest)} == {"unchanged"}

    # Multica's metadata survives; a resource deleted upstream does not linger.
    (dest / "ai-wiki" / "multica-metadata.json").write_text("{}")
    (dest / "ai-wiki" / "old-script.py").write_text("stale")
    assert next(r for r in skills.status(dest) if r["skill"] == "ai-wiki")["status"] == "stale"
    skills.install(dest, ("ai-wiki",))
    assert (dest / "ai-wiki" / "multica-metadata.json").read_text() == "{}"
    assert not (dest / "ai-wiki" / "old-script.py").exists()

    # An old link into a checkout is replaced by a real copy; what it pointed to is untouched.
    checkout = tmp_path / "checkout" / "ai-wiki"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("---\nname: ai-wiki\n---\nold\n")
    import shutil

    shutil.rmtree(dest / "ai-wiki")
    (dest / "ai-wiki").symlink_to(checkout)
    assert next(r for r in skills.status(dest) if r["skill"] == "ai-wiki")["status"] == "stale"
    skills.install(dest, ("ai-wiki",))
    assert not (dest / "ai-wiki").is_symlink() and (checkout / "SKILL.md").read_text().endswith("old\n")

    # Another skill's directory under our name is never overwritten, and nothing else is written either.
    shutil.rmtree(dest / "okf-knowledge-curator")
    (dest / "okf-knowledge-curator").mkdir()
    (dest / "okf-knowledge-curator" / "SKILL.md").write_text("---\nname: something-else\n---\n")
    (dest / "ai-wiki-auditor" / "SKILL.md").write_text("edited")
    with pytest.raises(skills.SkillError) as refused:
        skills.install(dest)
    assert refused.value.code == 2
    assert (dest / "ai-wiki-auditor" / "SKILL.md").read_text() == "edited"


def test_cli_skill_commands(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_SKILLS_HOME", str(tmp_path / "s"))
    assert cli.main(["skill"]) == 0
    out = capsys.readouterr().out
    assert 'status: "stale"' in out and "ai-wiki skill install" in out
    assert cli.main(["skill", "install", "ai-wiki"]) == 0
    assert (tmp_path / "s" / "ai-wiki" / "SKILL.md").is_file()
    assert not (tmp_path / "s" / "okf-knowledge-curator").exists()
    capsys.readouterr()
    with pytest.raises(SystemExit) as wrong:
        cli.main(["skill", "install", "nope"])
    assert wrong.value.code == 2


def test_update_check_reports_without_installing(monkeypatch, capsys) -> None:
    from aiwiki.cli import update

    monkeypatch.setattr(update, "_latest", lambda: "f" * 40)
    monkeypatch.setattr(update, "_receipt", lambda: {"commit": "a" * 40})
    called = []
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: called.append(a))
    assert cli.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "available: true" in out and "ai-wiki update" in out
    assert not called


def test_the_wheel_carries_the_skills(tmp_path: Path) -> None:
    pytest.importorskip("hatchling")
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                    "-w", str(tmp_path), str(ROOT)], check=True, capture_output=True)
    wheel = next(tmp_path.glob("ai_wiki-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for name in skills.NAMES:
        assert f"aiwiki/skills/{name}/SKILL.md" in names


def test_default_dir_follows_the_host_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AIWIKI_SKILLS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert skills.default_dir() == tmp_path / ".agents" / "skills"
    # A Multica daemon host keeps skill bodies in shared-skills; install and update must both use it.
    (tmp_path / ".agents" / "shared-skills").mkdir(parents=True)
    assert skills.default_dir() == tmp_path / ".agents" / "shared-skills"
    rows = skills.install()
    assert all(row["path"].startswith(str(tmp_path / ".agents" / "shared-skills")) for row in rows)
    assert not (tmp_path / ".agents" / "skills").exists()
    assert {row["status"] for row in skills.status()} == {"current"}
    monkeypatch.setenv("AIWIKI_SKILLS_HOME", str(tmp_path / "custom"))
    assert skills.default_dir() == tmp_path / "custom"
