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

from aiwiki import skills
from aiwiki.cli import main as cli
from aiwiki.cli import review
from aiwiki.maint import issue_delta

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ai-wiki-curating-maintainer" / "SKILL.md"
AUDITOR = ROOT / "skills" / "ai-wiki-auditor" / "SKILL.md"
PROMPT = ROOT / "docs" / "prompts" / "shadow-autopilot-prompt.md"
TEXTS = (SKILL, AUDITOR, ROOT / "skills" / "okf-knowledge-curator" / "SKILL.md", *sorted(PROMPT.parent.glob("*.md")),
         ROOT / "docs" / "external-agents.md", ROOT / "docs" / "final-cutover-runbook.md")
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
    if len(words) == 1 and words[0] in cli._VERSION_FLAGS:  # `ai-wiki --version`: answered before argparse
        return "--version", []
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
    # A budget, not a style rule: the whole skill is loaded into every maintainer run. 2026-10-10
    # took it from 220 to 230 for the issue-comment attribution rule and the `--deadline` parameter;
    # 2026-10-24 (0.3.4) to 250: the self-update preflight left 229/230, no room for the next small fix.
    # 2026-11-01 (0.3.9) to 260: the batch rule of §3.1 (one changeset curates several items).
    # 2026-11-04 (0.3.14) to 270: the pre-validate self-check of the auditor's top four refusals.
    # 2026-11-21 (0.3.41) to 280: capped-target sibling triage, pre-skip check, no backlog skip loosening, title bounds.
    assert len(text.splitlines()) <= 280
    verbs = {resolve(root, command)[0] for command in commands(text)}
    assert {"doctor", "maint begin", "maint next", "maint skip", "maint split", "maint add-evidence",
            "concept new", "validate", "propose", "workspace pull", "maint park", "maint end"} <= verbs


def test_the_auditor_skill_covers_its_loop_and_never_curates(root) -> None:
    text = AUDITOR.read_text(encoding="utf-8")
    assert text.startswith("---\nname: ai-wiki-auditor\n")
    # A budget, loaded into every audit run. 2026-10-24 (0.3.4) took it from 200 to 220: the
    # self-update preflight left 199/200, no room for the next small fix. 2026-11-01 to 230: the
    # Signals check of §3.3 (aliases, stale_after, supersession) left 226/220.
    # 2026-11-21 (0.3.41) to 240: reason: correction convergence rule (§3.1) and single-pass narrowing (§3.4).
    assert len(text.splitlines()) <= 240
    verbs = {resolve(root, command)[0] for command in commands(text)}
    assert verbs == {"update", "skill", "--version", "doctor", "review begin", "review next", "review evidence",
                     "review verdict", "review submit", "review triage next", "review triage verdict",
                     "review triage submit", "review end"}
    prompt = (PROMPT.parent / "auditor-autopilot-prompt.md").read_text(encoding="utf-8")
    assert "ai-wiki-auditor" in prompt and not {resolve(root, command)[0] for command in commands(prompt)} - verbs


def test_the_auditor_run_budget_is_the_prompt_max_alone() -> None:
    """2026-11-13/14: three audit runs begun with --max 100 stopped after 20 concepts, the agent
    citing the skill's "at most 20". The skill names no count of its own: `review next` ends the
    loop (exit 10 or 11), and the prompt's max is the one number."""
    text = AUDITOR.read_text(encoding="utf-8")
    preamble = " ".join(text.split("## 1.", 1)[0].split())
    # What the skill says `max` is: no number of its own, once the exit codes it names are set aside.
    said = re.search(r"`max` \((.*?)\), `triage_max`", preamble)[1]
    # The CLI's own deadline and triage reserve may be named, as the CLI sets them (0.3.32 hard-coded "2-hour"
    # here, so a deadline change could leave the skill saying the old one); any other number is the agent's own.
    deadline = f"{review.DEADLINE_S // 60}-minute deadline"
    reserve = f"last {review.TRIAGE_RESERVE_S // 60} minutes"
    assert deadline in said and reserve in said, said
    cli_facts = rf"`[^`]*`|\b{deadline}\b|\b{reserve}\b|§\d+\.\d+"
    assert "never a count or clock of your own" in said and not re.search(r"\d", re.sub(cli_facts, "", said))
    assert not re.search(r"\bat most \d+ concepts\b", " ".join(text.split())), "the phrasing behind 2026-11-13/14"
    prompt = (PROMPT.parent / "auditor-autopilot-prompt.md").read_text(encoding="utf-8")
    assert re.search(r'^max=\d+', prompt, re.M) and '--max "$max"' in AUDITOR.read_text(encoding="utf-8")


def test_the_curator_signals_date_every_time_bound_type_and_never_undated_evidence() -> None:
    curator = ROOT / "skills" / "okf-knowledge-curator"
    text = curator.joinpath("SKILL.md").read_text(encoding="utf-8")
    signals = " ".join(text.split("## Signals", 1)[1].split("\n## ", 1)[0].split())
    taxonomy = curator.joinpath("assets", "templates", "SCHEMA.md").read_text(encoding="utf-8")
    monetization = re.search(r"^\| (Monetization[^|]*?) \|", taxonomy, re.M)[1].split(", ")
    for kind in ("Metric", "Experiment", *monetization):
        assert f"`{kind}`" in signals, f"Signals: stale_after is required on {kind} too"
    # A declared date reads fresh, the default never does: undated evidence must not buy freshness.
    assert "curation day" not in signals and "no date gets no `stale_after`" in signals
    # A description is read without its footnotes: no state past what its evidence proves.
    assert "a state only a comment reports stays out of it" in signals


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


def test_the_production_prompt_keeps_the_legacy_parameters_and_leaves_audits_to_the_auditor() -> None:
    production = (PROMPT.parent / "production-autopilot-prompt.md").read_text(encoding="utf-8")
    config = json.loads(re.search(r"```json\n(.*?)```", production, re.S)[1])
    shadow = json.loads(re.search(r"```json\n(.*?)```", PROMPT.read_text(encoding="utf-8"), re.S)[1])

    assert config["repos"] == shadow["repos"]  # the workspace parameters the legacy production prompt passed
    assert config["audits"] == {"resubmit": False}  # the external auditor owns audits
    assert config["issues"]["autopilot"] == shadow["issues"]["autopilot"]  # updated in place, same id
    # Neither maintainer, the auditor nor the retired shadow is ever a source.
    assert {"1dcccd34-e9e4-48c7-a0a3-32c061d4c284", "c10a1e06-8255-42ce-9b52-266aef42f2a6",
            "<Auditor agent id>"} == set(config["issues"]["exclude_agents"])
    assert re.search(r"^bundle=solvely-wiki\b", production, re.M) and re.search(r"^max_items=\d+", production, re.M)
    assert "--format md" in SKILL.read_text(encoding="utf-8")  # the verbatim report opens the comment


def _section(text: str, heading: str, stop_prefix: str) -> str:
    body = text.split(heading, 1)[1]
    assert stop_prefix in body, f"{stop_prefix!r} not found after {heading!r}"
    return " ".join(body.split(stop_prefix, 1)[0].split())


def test_installed_maintainer_and_curator_skills_guard_capped_triage_and_title_boundaries(tmp_path: Path) -> None:
    """2026-11-21 (0.3.41): 8-source capped duplicate_of misses (75-87% overturns), bulk skip
    loosening under backlog pressure, and protected-title overstatements (51% of hard unverified)."""
    rows = skills.install(tmp_path)
    assert {row["status"] for row in rows} == {"installed"}

    maint = (tmp_path / "ai-wiki-curating-maintainer" / "SKILL.md").read_text(encoding="utf-8")
    skip_bullet = _section(maint, "### 3.2 Triage", "\n- Too large for one pass")
    capped_clause = _section(skip_bullet, "When the evidence adds durable knowledge it lacks", "A changed value")
    assert "8 or more sources (`too_many_sources`)" in capped_clause
    assert "its body is past 20000 characters (`body_too_long`)" in capped_clause
    assert "dated sibling concept (`<topic>-<reading>-<date>`)" in capped_clause
    assert "instead of marking `duplicate_of`" in capped_clause
    assert "Code Review or QA round" in capped_clause
    assert "telemetry caveats" in capped_clause and "acceptance results" in capped_clause

    preskip_clause = _section(skip_bullet, "Before `no_durable_knowledge` or `out_of_scope`", "Never loosen skip")
    assert "check every frozen file against existing concept(s)" in preskip_clause
    assert "telemetry caveats" in preskip_clause and "deferred findings" in preskip_clause
    assert "acceptance results" in preskip_clause

    queue_clause = skip_bullet.split("Never loosen skip", 1)[1]
    assert "or `duplicate_of` judgment to drain a large `ready` queue" in queue_clause

    curate = _section(maint, "### 3.3 Curate in the workspace", "\n### 3.4 ")
    assert "never states a reported claim as bare fact" in curate
    assert "dev/test-only mock or code-only read as an unqualified production fact" in curate
    assert "a review cannot retitle" in curate

    curator = (tmp_path / "okf-knowledge-curator" / "SKILL.md").read_text(encoding="utf-8")
    provenance = _section(curator, "## Provenance and per-claim attribution", "\n## ")
    assert "`title` is identity-locked and cannot be narrowed by audit" in provenance
    assert "dev/test mocks, code-only reads and reported claims" in provenance
    assert "prefer a neutral topic title for reported claims" in provenance
    assert "bare or unqualified production facts" in provenance


def test_installed_auditor_skill_converges_correction_rereviews_and_narrows_in_one_pass(tmp_path: Path) -> None:
    """2026-11-21 (0.3.41): prevent self-recorrection churn on `reason: correction` (42-77% repeat
    corrections with 6-line median diffs) while keeping full factual rigor on material claims."""
    rows = skills.install(tmp_path)
    assert {row["status"] for row in rows} == {"installed"}

    auditor = (tmp_path / "ai-wiki-auditor" / "SKILL.md").read_text(encoding="utf-8")
    take = _section(auditor, "### 3.1 Take", "\n### 3.2 ")
    assert "Judge it like new content" not in take
    assert "same factual standard" in take
    assert "On `reason: correction`" in take
    assert (
        "every material claim in the concept and `description` is already supported, uncontradicted and bounded" in take
    )
    assert "record `verified` even when phrasing could be tighter" in take
    assert "or `unverified` per §3.3/§3.4 when it cannot be narrowed" in take
    assert "overstated `title` or a `related` contradiction" in take
    assert "remaining material defect that could mislead a reader" in take
    assert "never for wording polish or formatting/style tweaks" in take

    verdict = _section(auditor, "### 3.4 Record a verdict", "\n### 3.5 ")
    assert "Audit the whole concept and narrow every unsupported or overstated claim in one pass" in verdict
    assert "rather than stopping after one sentence" in verdict and "12 h cycle" in verdict

    prompt = (PROMPT.parent / "auditor-autopilot-prompt.md").read_text(encoding="utf-8")
    assert "像审新内容一样" not in prompt
    narrow_bullet = next(line for line in prompt.splitlines() if line.startswith("- 修正只能收窄："))
    assert "一次性收窄全部过强或无依据的表述" in narrow_bullet and "不要只改一句留给下一轮" in narrow_bullet
    corr_bullet = next(line for line in prompt.splitlines() if line.startswith("- `reason: correction` "))
    assert "按同等事实标准对照它引用的证据" in corr_bullet
    assert "所有实质主张都已有依据、无矛盾且边界清楚" in corr_bullet and "直接记 verified" in corr_bullet
    assert "按 Skill §3.3/§3.4 记 unverified" in corr_bullet
    assert "不要为措辞润色或格式微调再次修正" in corr_bullet
