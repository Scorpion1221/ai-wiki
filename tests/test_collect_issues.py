"""The Multica issues collector against the fake multica CLI (design §4.3, §10.1, W8).

Covers the exclusion rules, redaction before freezing, splitting by comment window, and the
``unavailable``/``failed`` paths that must leave the issues cursor where it was. Response
shapes follow multica 0.4.35 as read on 2026-09-24 (comment ``type`` is ``comment`` or
``system``; ``issue timeline --action a,b`` returns activity rows, and ``description_updated``
has empty ``details``).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwiki.maint import collect_issues, planner
from aiwiki.service import maint_state

FAKE = Path(__file__).with_name("fake_multica.py")
AUTOPILOT = "5c80732b-67a6-4e33-ba22-c620a94e27c1"
MAINTAINER = "1dcccd34-e9e4-48c7-a0a3-32c061d4c284"
AUDITOR = "a0d17000-0000-4000-8000-000000000001"
ANALYST = "1ef2f6e3-738f-406b-87f9-5632bff6115a"
MEMBER = "m-0001"
CURSOR = {"updated_at": "2026-09-19T16:40:04Z", "id": "01a0a49d-579b-7379-9d1a-ada7254035f8"}


@pytest.fixture
def multica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put tests/fake_multica.py on PATH as ``multica``; returns its state file."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "multica"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    state = tmp_path / "multica-state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_MULTICA_STATE", str(state))
    monkeypatch.delenv("MULTICA_ISSUE_ID", raising=False)
    return state


def save(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def calls(state_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in Path(f"{state_path}.calls").read_text(encoding="utf-8").splitlines()]


def issue(number: int, created: str, updated: str, **extra: Any) -> dict[str, Any]:
    return {"id": f"issue-{number}", "identifier": f"WAIO-{number}", "title": f"title {number}",
            "status": "in_review", "description": f"description {number}", "created_at": created,
            "updated_at": updated, "last_activity_at": updated, "metadata": {}, "parent_issue_id": None,
            "assignee_type": "agent", "assignee_id": ANALYST, **extra}


def comment(comment_id: str, created: str, content: str, author: str = MEMBER, **extra: Any) -> dict[str, Any]:
    return {"id": comment_id, "author_id": author, "author_type": "member" if author == MEMBER else "agent",
            "content": content, "created_at": created, "revision": 1, "type": "comment", **extra}


def status_change(created: str, old: str, new: str) -> dict[str, Any]:
    return {"action": "status_changed", "actor_type": "member", "created_at": created,
            "details": {"from": old, "to": new}, "type": "activity"}


def edit(created: str, action: str, **details: str) -> dict[str, Any]:
    return {"action": action, "actor_type": "member", "created_at": created, "details": details, "type": "activity"}


def run(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    return collect_issues.collect(CURSOR, autopilot=AUTOPILOT, cache_dir=tmp_path / "cache",
                                  exclude_agents=[MAINTAINER, AUDITOR], **kwargs)


def test_collect_excludes_maintenance_issues_and_agent_output(multica: Path, tmp_path: Path) -> None:
    save(multica, {
        "runs": [{"issue_id": "issue-600", "status": "completed"}],
        "issues": [
            issue(600, "2026-09-20T20:00:00Z", "2026-09-20T21:00:00Z", assignee_id=MAINTAINER),  # autopilot run
            issue(601, "2026-09-01T00:00:00Z", "2026-09-21T00:00:00Z",
                  metadata={"ai_wiki_incremental_checkpoint_v4": "{}"}),  # a maintenance report
            issue(602, "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z", assignee_id=MAINTAINER),
            issue(603, "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z", assignee_id=AUDITOR),
            issue(604, "2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z", assignee_type="member", assignee_id=MEMBER,
                  creator_type="agent", creator_id=MAINTAINER),  # its description is the Maintainer's text
            issue(610, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z", assignee_type="member", assignee_id=MEMBER),
            issue(611, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z"),  # only a system comment in the window
            issue(612, "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"),  # new, nothing else yet
            issue(613, "2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z"),  # unchanged since the cursor
        ],
        "comments": {
            "issue-610": [
                comment("c-old", "2026-09-18T00:00:00Z", "before the cursor"),
                comment("c-member", "2026-09-20T01:00:00Z", "Decision: pin the release to master."),
                comment("c-analyst", "2026-09-20T02:00:00Z", "结论：已按 master 发布。", author=ANALYST,
                        parent_id="c-member"),
                comment("c-maintainer", "2026-09-20T03:00:00Z", "wiki updated", author=MAINTAINER),
                comment("c-system", "2026-09-20T04:00:00Z", "You've hit your usage limit.", author=ANALYST,
                        type="system"),
            ],
            "issue-611": [comment("c-limit", "2026-09-22T00:00:00Z", "usage limit", author=ANALYST, type="system")],
        },
        "timelines": {"issue-610": [status_change("2026-09-11T00:00:00Z", "todo", "in_progress"),
                                    status_change("2026-09-21T00:00:00Z", "in_review", "done")]},
    })

    result = run(tmp_path)

    assert result["status"] == "ok" and result["error"] is None
    assert result["next_cursor"] == {"updated_at": "2026-09-22T00:00:00Z", "id": "issue-611"}
    assert result["counts"] == {
        "listed": 9, "changed": 8, "deferred": 0,
        "excluded": {"agent_assignee": 2, "agent_creator": 1, "autopilot_run": 1, "maintenance_metadata": 1},
        "quiet": 1, "comments": 2, "system_comments": 2, "agent_comments_excluded": 1, "items": 2,
    }
    # In window order (changed_at, id), like issue_delta's candidates.
    second, first = result["candidates"]
    assert (second["topic_key"], first["topic_key"]) == ("issue:WAIO-612", "issue:WAIO-610")
    assert {key: value for key, value in first["origin"].items() if key != "until"} == {
        "kind": "issue", "issue_id": "issue-610", "identifier": "WAIO-610", "status": "in_review",
        "after": CURSOR["updated_at"], "deferred": False}
    assert first["signals"] == {"decision": True, "status_changes": 1}
    assert first["brief"] == ("WAIO-610 [in_review] title 610; 2 comments (1 by members); "
                              "status in_review → done; decision")
    [file] = first["files"]
    text = file["data"].decode()
    assert file["name"] == "WAIO-610-20260919T164004Z.md"
    assert file["origin"] == {"kind": "issue", "issue_id": "issue-610", "identifier": "WAIO-610",
                              "comments": ["c-member", "c-analyst"], "truncated": False, "redactions": 0}
    assert "## Description\n\ndescription 610" in text
    assert "- 2026-09-21T00:00:00Z in_review → done (member)" in text and "todo → in_progress" not in text
    assert "Decision: pin the release to master." in text and "结论：已按 master 发布。" in text
    assert "reply to c-member" in text
    for dropped in ("before the cursor", "wiki updated", "usage limit"):
        assert dropped not in text
    assert second["signals"] == {"decision": False, "status_changes": 0}
    assert [(item["topic_key"], item["priority"]) for item in planner.plan(result["candidates"])] == [
        ("issue:WAIO-610", 60), ("issue:WAIO-612", 40),
    ]

    commands = [call[:2] for call in calls(multica)]
    assert set(map(tuple, commands)) == {("autopilot", "runs"), ("issue", "list"), ("issue", "comment"),
                                         ("issue", "timeline")}, "the collector only reads, never run messages"
    assert (tmp_path / "cache" / "timelines" / "issue-610.json").is_file()


def test_issue_evidence_refuses_the_issues_the_collector_excludes(multica: Path, tmp_path: Path) -> None:
    at = ("2026-09-20T00:00:00Z", "2026-09-20T01:00:00Z")
    save(multica, {
        "runs": [{"issue_id": "issue-600", "status": "completed"}],
        "issues": [
            issue(600, *at, assignee_type="member", assignee_id=MEMBER),  # an autopilot run's issue
            issue(601, *at, metadata={"ai_wiki_incremental_checkpoint_v4": "{}"}),  # a maintenance report
            issue(602, *at, assignee_id=AUDITOR),  # a handoff issue
            issue(604, *at, assignee_type="member", assignee_id=MEMBER, creator_type="agent", creator_id=MAINTAINER),
            issue(605, *at), issue(606, *at),
            issue(610, *at),
        ],
        "comments": {"issue-610": [comment("c-member", at[1], "Decision: ship it."),
                                   comment("c-maintainer", at[1], "wiki updated", author=MAINTAINER)]},
    })

    def evidence(identifier: str, **config: Any) -> dict[str, Any]:
        settings = {"autopilot": AUTOPILOT, "exclude_agents": [MAINTAINER, AUDITOR], **config}
        return collect_issues.issue_evidence(identifier, None, cache_dir=tmp_path / identifier,
                                             current_issue="issue-605", exclude_issues=["issue-606"], **settings)

    for number, reason in ((600, "autopilot_run"), (601, "maintenance_metadata"), (602, "agent_assignee"),
                           (604, "agent_creator"), (605, "current_issue"), (606, "explicit")):
        with pytest.raises(ValueError, match=reason):
            evidence(f"WAIO-{number}")
    text = evidence("WAIO-610")["data"].decode()
    assert "Decision: ship it." in text and "wiki updated" not in text
    for unset in ({"autopilot": None}, {"exclude_agents": []}):  # nothing to check against: fail closed
        with pytest.raises(ValueError, match="issues.autopilot and issues.exclude_agents"):
            evidence("WAIO-610", **unset)


def test_collect_keeps_title_and_description_edits(multica: Path, tmp_path: Path) -> None:
    rewritten = "REWRITTEN SPEC: we decided to drop the weekly plan"
    save(multica, {
        "issues": [issue(701, "2026-09-01T00:00:00Z", "2026-09-20T01:00:00Z", description=rewritten),
                   issue(702, "2026-09-01T00:00:00Z", "2026-09-20T02:00:00Z", title="Weekly plan v2")],
        "timelines": {
            "issue-701": [edit("2026-09-02T00:00:00Z", "title_changed", **{"from": "a", "to": "b"}),
                          edit("2026-09-20T01:00:00Z", "description_updated")],
            "issue-702": [
                edit("2026-09-20T02:00:00Z", "title_changed", **{"from": "Weekly plan", "to": "Weekly plan v2"}),
                edit("2026-09-20T02:00:00Z", "priority_changed", **{"from": "low", "to": "high"}),
            ],
        },
    })

    result = run(tmp_path)

    assert (result["counts"]["quiet"], result["counts"]["items"]) == (0, 2)
    assert result["next_cursor"] == {"updated_at": "2026-09-20T02:00:00Z", "id": "issue-702"}
    description, title = result["candidates"]
    assert description["brief"] == "WAIO-701 [in_review] title 701; 0 comments (0 by members); edited description"
    text = description["files"][0]["data"].decode()
    assert f"## Description\n\n{rewritten}" in text
    assert "## Edits\n\n- 2026-09-20T01:00:00Z description_updated (member)\n" in text
    assert "title_changed" not in text, "the edit before the cursor is out of the window"
    assert title["brief"].endswith("; edited title")
    text = title["files"][0]["data"].decode()
    assert "- 2026-09-20T02:00:00Z title_changed: Weekly plan → Weekly plan v2 (member)" in text
    assert "priority_changed" not in text
    assert ["issue", "timeline", "issue-701", "--action", "status_changed,title_changed,description_updated",
            "--output", "json"] in calls(multica)


def test_collect_redacts_secrets_before_freezing(multica: Path, tmp_path: Path) -> None:
    token, key = "ghp_" + "Z" * 36, "sk-" + "b" * 32
    save(multica, {"issues": [issue(620, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z",
                                    title=f"Rotate leaked key {key}", description=f"deploy with {token}")],
                   "comments": {"issue-620": [comment("c1", "2026-09-21T00:00:00Z",
                                                      "https://deploy:s3cr3t-pass@git.example.com/x\n"
                                                      "Refresh token:\nrotate every 30 days")]}})

    [candidate] = run(tmp_path)["candidates"]

    [file] = candidate["files"]
    text = file["data"].decode()
    for secret in (token, key, "s3cr3t-pass"):
        assert secret not in text and secret not in planner.plan([candidate])[0]["brief"]
    assert file["origin"]["redactions"] == 3  # title, description, and the URL credential in the comment
    assert "Refresh token:\nrotate every 30 days" in text


def test_collect_packs_redacted_text_so_a_longer_marker_never_clips(multica: Path, tmp_path: Path) -> None:
    row = issue(800, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z")
    first = comment("c1", "2026-09-20T01:00:00Z", "clone https://u:abc@git.example.com/x\n")
    last = comment("c2", "2026-09-20T02:00:00Z", "FINAL CONCLUSION: ship on Friday")
    raw = collect_issues.evidence_blocks(row, "WAIO-800", CURSOR["updated_at"], [first, last], [], [])
    # One byte under the file limit as written; the redaction marker is 21 bytes longer than u:abc.
    first["content"] += "p" * (planner.FILE_TEXT_LIMIT - 1 - sum(planner.utf8_size(text) for text, _ in raw))
    save(multica, {"issues": [row], "comments": {"issue-800": [first, last]}})

    [candidate] = run(tmp_path)["candidates"]

    assert [file["name"] for file in candidate["files"]] == ["WAIO-800-20260919T164004Z-1.md",
                                                             "WAIO-800-20260919T164004Z-2.md"]
    assert [(file["origin"]["truncated"], file["origin"]["redactions"]) for file in candidate["files"]] == [
        (False, 1), (False, 0)]
    assert all(file["bytes"] <= planner.FILE_TEXT_LIMIT for file in candidate["files"])
    assert candidate["files"][1]["data"].decode().endswith("FINAL CONCLUSION: ship on Friday\n")


def test_collect_splits_a_long_issue_into_parts_by_comment_window(multica: Path, tmp_path: Path) -> None:
    comments = [comment(f"c{index:02d}", f"2026-09-20T{index:02d}:00:00Z", f"{index} " + "x" * 10_000)
                for index in range(12)]
    comments.append(comment("c-huge", "2026-09-20T13:00:00Z", "y" * 100_000))
    save(multica, {"issues": [issue(630, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z")],
                   "comments": {"issue-630": comments}})

    candidates = run(tmp_path)["candidates"]

    assert [candidate["topic_key"] for candidate in candidates] == [
        "issue:WAIO-630#part-1", "issue:WAIO-630#part-2", "issue:WAIO-630#part-3"]
    files = [file for candidate in candidates for file in candidate["files"]]
    assert [file["name"] for file in files] == [f"WAIO-630-20260919T164004Z-{index}.md" for index in range(1, 8)]
    assert [file["origin"]["comments"] for file in files] == [
        ["c00", "c01"], ["c02", "c03"], ["c04", "c05"], ["c06", "c07"], ["c08", "c09"], ["c10", "c11"], ["c-huge"]]
    assert [file["origin"]["truncated"] for file in files] == [False] * 6 + [True]
    assert all(file["bytes"] <= planner.FILE_TEXT_LIMIT for file in files)
    assert all(sum(file["bytes"] for file in candidate["files"]) <= planner.ITEM_TEXT_LIMIT
               for candidate in candidates)
    assert candidates[1]["origin"]["part"] == 2 and candidates[1]["origin"]["parts"] == 3
    assert candidates[1]["brief"].startswith("part 2/3 · WAIO-630 [in_review]")
    assert "## Description" in files[0]["data"].decode() and "## Description" not in files[1]["data"].decode()
    # Collecting the same window again yields the same evidence, so the writer keys the same items.
    again = collect_issues.collect(CURSOR, autopilot=AUTOPILOT, cache_dir=tmp_path / "cache-2")["candidates"]

    def keys(found: list[dict]) -> list[str]:
        return [maint_state.item_key(item["origin"]["kind"], item["topic_key"], [f["sha256"] for f in item["files"]])
                for item in planner.plan(found)]

    assert keys(again) == keys(candidates)


def test_consecutive_windows_freeze_distinct_files_so_a_merge_keeps_both(multica: Path, tmp_path: Path) -> None:
    decision = comment("c-decision", "2026-09-20T01:00:00Z", "Decision: pin the release to master.")
    state = {"issues": [issue(700, "2026-09-10T00:00:00Z", "2026-09-20T01:00:00Z")],
             "comments": {"issue-700": [decision]}}
    save(multica, state)
    first = run(tmp_path)
    state["issues"] = [issue(700, "2026-09-10T00:00:00Z", "2026-09-21T00:00:00.25Z")]
    state["comments"]["issue-700"].append(comment("c-ok", "2026-09-21T00:00:00.25Z", "ok"))
    save(multica, state)

    second = collect_issues.collect(first["next_cursor"], autopilot=AUTOPILOT, cache_dir=tmp_path / "cache-2",
                                    exclude_agents=[MAINTAINER, AUDITOR])
    third = collect_issues.collect(second["next_cursor"] | {"updated_at": "2026-09-20T12:00:00.5Z"},
                                   autopilot=AUTOPILOT, cache_dir=tmp_path / "cache-3")

    [[old]], [[new]], [[later]] = ([c["files"] for c in result["candidates"]] for result in (first, second, third))
    assert first["candidates"][0]["topic_key"] == second["candidates"][0]["topic_key"] == "issue:WAIO-700"
    assert (old["name"], new["name"]) == ("WAIO-700-20260919T164004Z.md", "WAIO-700-20260920T010000Z.md")
    assert later["name"] == "WAIO-700-20260920T120000.500000Z.md"
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", file["name"]) for file in (old, new, later))
    # The queue merges a ready item's same-topic collection by file name: both windows survive.
    merged = {file["name"]: file["data"].decode() for file in (old, new)}
    assert "Decision: pin the release to master." in merged[old["name"]]
    assert "ok" in merged[new["name"]] and "Decision" not in merged[new["name"]]


def test_collect_is_unavailable_without_multica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    result = run(tmp_path)

    assert result == {"status": "unavailable", "error": "multica CLI not found on PATH", "cursor": CURSOR,
                      "next_cursor": CURSOR, "counts": {}, "candidates": []}


@pytest.mark.parametrize(("fail", "message"), [
    ("issue list", "503 Service Unavailable"),
    ("issue timeline issue-640", "503 Service Unavailable"),
])
def test_collect_failure_keeps_the_issues_cursor(multica: Path, tmp_path: Path, fail: str, message: str) -> None:
    save(multica, {"issues": [issue(640, "2026-09-10T00:00:00Z", "2026-09-22T00:00:00Z")],
                   "comments": {"issue-640": [comment("c1", "2026-09-21T00:00:00Z", "hello")]},
                   "fail": {fail: f"Error: request failed: {message}"}})

    result = run(tmp_path)

    assert (result["status"], result["next_cursor"], result["candidates"]) == ("failed", CURSOR, [])
    assert message in result["error"]


def test_collect_requires_a_cursor(multica: Path, tmp_path: Path) -> None:
    result = collect_issues.collect({}, autopilot=AUTOPILOT, cache_dir=tmp_path / "cache")

    assert result["status"] == "failed"
    assert result["error"] == "issues cursor {updated_at, id} is required"
