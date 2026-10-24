"""The agent skills ship with the CLI: stamped with its version, installed by it, updated with it."""
from __future__ import annotations

import subprocess
import sys
import time
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
    # Owner decision 2026-10-24: agents update the CLI themselves, shared Multica host included; the CLI
    # refuses under a live run, and an agent whose attached (workspace) Skill lags the CLI reads the installed one.
    import re

    behind = '"workspace Skill is behind the CLI; publish it"'
    prompts = [ROOT / "docs" / "prompts" / f"{role}-agent-instructions.md"
               for role in ("production", "shadow", "auditor")]
    for path in [ROOT / "skills" / name / "SKILL.md" for name in skills.NAMES] + prompts:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
        text = " ".join(path.read_text("utf-8").split())
        assert re.search(r"`ai-wiki update --check[^`]*`", text), f"{name}: its preflight runs `ai-wiki update --check`"
        assert "never reinstall" not in text.lower(), f"{name}: contradicts the self-update rule"
        assert "--token" not in text, f"{name}: a token goes in $AIWIKI_TOKEN, never in a command"
        assert "`<!-- ai-wiki:version X -->`" in text and "`ai-wiki --version`" in text and behind in text, \
            f"{name}: after an update, compare the followed Skill's stamp with the CLI, else stop blocked"
        assert "never retry" in text and "install by hand" in text, f"{name}: a failed update is reported, not retried"
        if name != "okf-knowledge-curator":   # it ships with ai-wiki; its callers' preflights update it
            assert re.search(r"`ai-wiki update[^`]*&& hash -r`", text) and "Never update " in text, \
                f"{name}: update and rehash before the run, never during its lease"


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


@pytest.fixture
def updater(monkeypatch, tmp_path):
    """`ai-wiki update` against a temp state dir, with main ahead and every subprocess recorded, never run."""
    from aiwiki.cli import maint, update

    state = tmp_path / "state"
    monkeypatch.setattr(update, "_default_state_dir", lambda: state)
    monkeypatch.setattr(update, "_latest", lambda: "f" * 40)
    monkeypatch.setattr(update, "_receipt", lambda: {"commit": "a" * 40})
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("AIWIKI_SKILLS_HOME", str(tmp_path / "skills"))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        out = 'installed[1]{skill,status,skill_md}:\n  "ai-wiki","updated","/s/ai-wiki/SKILL.md"' \
            if argv[1:] == ["skill", "install"] else "0.3.4"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(update.subprocess, "run", run)

    def begin(kind: str, bundle: str = "kb", run_id: str = "WAIO-1") -> Path:
        folder = "runs" if kind == "maint" else "reviews"
        maint._write(state / folder / f"current-{maint._slug(bundle)}.json", {"run": run_id, "bundle": bundle})
        maint._write(state / folder / maint._slug(run_id) / ("run.json" if kind == "maint" else "review.json"), {})
        return state / folder

    return state, calls, begin


@pytest.mark.parametrize("kind", ["maint", "review"])
def test_update_refuses_under_a_live_run_and_installs_nothing(updater, capsys, kind) -> None:
    _state, calls, begin = updater
    begin(kind)
    assert cli.main(["update"]) == 1
    out = capsys.readouterr().out
    assert 'code: "run_live"' in out and "before your next run (never during one)" in out
    assert f'"{kind}","kb","WAIO-1",0' in out
    assert calls == []                                       # neither uv nor the skill install ran


def test_update_goes_ahead_past_a_run_older_than_the_lease(updater, capsys) -> None:
    import os

    from aiwiki.service.maint_state import LEASE_TTL

    _state, calls, begin = updater
    old = time.time() - LEASE_TTL.total_seconds() - 60      # died without `end`: its lease has expired
    for path in begin("maint").rglob("*"):
        os.utime(path, (old, old))
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert [argv[1:3] for argv in calls][:2] == [["tool", "install"], ["skill", "install"]]
    assert "/s/ai-wiki/SKILL.md" in out                      # the new code's install table, with each SKILL.md


def test_update_refuses_while_a_maintain_or_another_update_holds_its_lock(updater, tmp_path, capsys) -> None:
    import fcntl

    state, calls, _begin = updater
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    for path, argv, code in ((ledger / "runner.lock", ["update", "--state-dir", str(ledger)], "run_live"),
                             (state / "update.lock", ["update"], "update_running")):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert cli.main(argv) == 1
            assert f'code: "{code}"' in capsys.readouterr().out
    assert calls == []
    assert cli.main(["update", "--state-dir", str(ledger)]) == 0  # both released: it installs


def test_update_check_names_the_live_run_and_offers_no_update(updater, capsys) -> None:
    _state, calls, begin = updater
    begin("review", bundle="kb-shadow", run_id="WAIO-2")
    assert cli.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "available: true" in out and '"review","kb-shadow","WAIO-2",0' in out
    assert "help[" not in out and "before your next run (never during one)" in out
    assert "SKILL.md" in out                                 # where each installed skill is read from
    assert calls == []


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
