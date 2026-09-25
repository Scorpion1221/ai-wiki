"""The curating maintainer's skill and prompts name only real CLI verbs and flags (design §13 W13).

An agent runs these texts verbatim, so a renamed verb or flag would fail a scheduled run; each
command is walked through the CLI's own parser tree instead of a copy of it.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

import pytest

from aiwiki.cli import main as cli
from aiwiki.maint import issue_delta

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ai-wiki-curating-maintainer" / "SKILL.md"
PROMPT = ROOT / "docs" / "prompts" / "shadow-autopilot-prompt.md"
TEXTS = (SKILL, ROOT / "skills" / "okf-knowledge-curator" / "SKILL.md", *sorted(PROMPT.parent.glob("*.md")))
SPAN = re.compile(r"`(ai-wiki [^`]+)`")
END = re.compile(r"\s(?:[|;>]|&&|2>)\s|\s#\s")  # a pipe, chain, redirect or shell comment ends a command


@pytest.fixture
def root(monkeypatch) -> argparse.ArgumentParser:
    """The root parser ``ai-wiki`` builds, captured before it parses anything."""
    built = []

    def capture(self, *_args, **_kwargs):
        built.append(self)
        raise SystemExit(0)

    monkeypatch.setattr(cli._AxiParser, "parse_args", capture)
    with pytest.raises(SystemExit):
        cli.main(["health"])
    return built[0]


def commands(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("ai-wiki ")]
    spans = [" ".join(span.split()) for span in SPAN.findall(text)]
    return [END.split(command)[0] for command in lines + spans]


def resolve(root: argparse.ArgumentParser, command: str) -> tuple[str, list[str]]:
    """The verb path a command reaches and its unknown ``--flags``; fails on an unknown verb."""
    words, parser, path = shlex.split(command)[1:], root, []
    while True:
        if words[:1] == ["-b"]:
            words = words[2:]
            continue
        verbs = next((action.choices for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction)), None)
        if verbs is None:
            break
        assert words and words[0] in verbs, f"{command}: no verb {words[:1]} under {' '.join(path) or 'ai-wiki'}"
        parser, path, words = verbs[words[0]], [*path, words[0]], words[1:]
    flags = {word.strip("[]").split("=")[0] for word in words if word.strip("[]").startswith("--")}
    return " ".join(path), sorted(flags - set(parser._option_string_actions))


@pytest.mark.parametrize("text", TEXTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_command_resolves_to_a_real_verb_and_flags(root, text: Path) -> None:
    for command in commands(text.read_text(encoding="utf-8")):
        verb, unknown = resolve(root, command)
        assert not unknown, f"{text.name}: {command!r} passes {unknown} that ai-wiki {verb} does not take"


def test_the_skill_covers_the_whole_loop(root) -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\nname: ai-wiki-curating-maintainer\n")
    assert len(text.splitlines()) <= 220
    verbs = {resolve(root, command)[0] for command in commands(text)}
    assert {"doctor", "maint begin", "maint next", "maint skip", "maint split", "maint add-evidence",
            "concept new", "validate", "propose", "workspace pull", "maint park", "maint end"} <= verbs


def test_the_shadow_prompt_config_is_what_the_collectors_read() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    config = json.loads(re.search(r"```json\n(.*?)```", text, re.S)[1])

    # Production's legacy issue delta skips only issues carrying this prefix, not the shadow's own.
    assert re.search(r"multica issue metadata set \S+ --key (\w+)", text)[1].startswith(
        issue_delta.MAINTENANCE_METADATA_PREFIX)

    assert config["audits"] == {"resubmit": False}  # the shadow's audits are the admin cron's
    repos, issues = config["repos"], config["issues"]
    assert repos["root"] and repos["registry"] == "multica"
    assert repos["priority_prefixes"] == ["tasks", "memory", "docs/solutions"]
    assert set(repos["branch_overrides"].values()) == {"master"}
    assert set(repos["required_remotes"]) <= set(repos["branch_overrides"])
    assert issues["autopilot"] and len(issues["exclude_agents"]) == 2
