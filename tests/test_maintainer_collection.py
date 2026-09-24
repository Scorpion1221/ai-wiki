"""Checkpoint discovery/build/write and the Multica issue delta, against a fake multica CLI.

Fixture shapes mirror production responses read on 2026-09-24 (multica 0.4.35): metadata
values are JSON strings, ``metadata get`` encodes them once more, autopilot runs are newest
first, and the newest v4 (completed_at 2026-09-20T10:35:37Z, cursor 2026-09-19T16:40:04Z)
sits on a run whose status is ``failed`` because it was recovered after the run ended.
Hosts and names are placeholders.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "ai-wiki-maintainer" / "scripts"
FAKE = Path(__file__).with_name("fake_multica.py")
AUTOPILOT = "5c80732b-67a6-4e33-ba22-c620a94e27c1"

_spec = importlib.util.spec_from_file_location("scan_reference_repos", SCRIPTS / "scan_reference_repos.py")
assert _spec and _spec.loader
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


@dataclass
class FakeMultica:
    state_path: Path

    def load(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = Path(f"{self.state_path}.calls")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def multica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMultica:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "multica"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    state = tmp_path / "multica-state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_MULTICA_STATE", str(state))
    monkeypatch.delenv("MULTICA_ISSUE_ID", raising=False)
    return FakeMultica(state)


def script(name: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=cwd or ROOT,
                          capture_output=True, text=True, check=False, timeout=60)


def repo_row(name: str, sha_char: str, branch: str = "master", **extra: str) -> tuple[str, dict[str, str]]:
    remote = f"https://code.example.com/group/{name}.git"
    return scanner.repo_id(scanner.canonical_remote(remote)), {
        "branch": branch, "name": name, "remote_url": remote, "sha": sha_char * 40, **extra,
    }


def v4(completed_at: str, cursor_at: str, cursor_id: str, *rows: tuple[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "version": 4,
        "repo_root": "/home/ubuntu/git/reference-repos/control",
        "repos": dict(rows or [repo_row("web-server", "a"), repo_row("public-server", "b", "main")]),
        "issues": {"updated_at": cursor_at, "id": cursor_id},
        "completed_at": completed_at,
    }


def v3(completed_at: str) -> dict[str, Any]:
    return {
        "version": 3,
        "repo_root": "/home/ubuntu/git/reference-repos/control/",
        "repos": {"web-server": {"remote_url": "https://code.example.com/group/web-server.git",
                                 "branch": "master", "sha": "c" * 40}},
        "issues": {"updated_at": "2026-09-02T20:01:13Z", "id": "01a063b5-62c8-7f34-8901-580e722d9532"},
        "completed_at": completed_at,
    }


def stored(checkpoint: dict[str, Any]) -> str:
    return json.dumps(checkpoint, separators=(",", ":"))


LATEST = v4("2026-09-20T10:35:37Z", "2026-09-19T16:40:04Z", "01a0a49d-579b-7379-9d1a-ada7254035f8")


def production_like_state() -> dict[str, Any]:
    malformed = v4("2026-09-21T09:00:00Z", "2026-09-21T08:00:00Z", "x")
    next(iter(malformed["repos"].values()))["sha"] = "not-a-sha"
    return {
        "runs": [
            {"issue_id": "issue-0922", "status": "failed", "created_at": "2026-09-22T20:00:31Z"},
            {"issue_id": "issue-0921", "status": "failed", "created_at": "2026-09-21T20:00:31Z"},
            {"issue_id": "issue-0919", "status": "failed", "created_at": "2026-09-19T20:00:26Z"},
            {"issue_id": "issue-0918", "status": "issue_created", "created_at": "2026-09-18T20:00:23Z"},
            {"issue_id": "issue-0917", "status": "completed", "created_at": "2026-09-17T20:00:16Z"},
            {"issue_id": "issue-0910", "status": "failed", "created_at": "2026-09-10T20:00:00Z"},
            {"issue_id": "issue-0907", "status": "failed", "created_at": "2026-09-07T20:00:00Z"},
            {"issue_id": "issue-0902", "status": "completed", "created_at": "2026-09-02T20:00:00Z"},
        ],
        "metadata": {
            "issue-0921": {"ai_wiki_incremental_checkpoint_v4": stored(malformed)},
            "issue-0919": {"ai_wiki_incremental_checkpoint_v4": stored(LATEST)},
            "issue-0917": {"ai_wiki_incremental_checkpoint_v4": stored(
                v4("2026-09-17T21:32:32Z", "2026-09-17T19:30:15Z", "01a0b025-995e-75e4-82fc-22e87afafdaa"))},
            "issue-0910": {"ai_wiki_recovery_receipt": json.dumps({"status": "completed"})},
            "issue-0907": {"ai_wiki_incremental_checkpoint_v4": stored(
                v4("2026-09-08T04:15:09Z", "2026-09-07T20:04:43Z", "01a07d78-b443-7371-9985-0f2a4b4caeb0"))},
            "issue-0902": {"ai_wiki_incremental_checkpoint_v3": stored(v3("2026-09-02T20:52:31Z"))},
        },
    }


def test_find_picks_latest_valid_v4_by_completed_at_not_run_status(multica: FakeMultica, tmp_path: Path) -> None:
    multica.save(production_like_state())
    cache = tmp_path / "cache"

    result = script("checkpoint.py", "find", "--autopilot", AUTOPILOT, "--cache-dir", str(cache))

    assert result.returncode == 0, result.stdout + result.stderr
    found = json.loads(result.stdout)
    assert found["found"] is True
    assert (found["version"], found["fallback"], found["issue_id"]) == (4, False, "issue-0919")
    assert found["completed_at"] == "2026-09-20T10:35:37Z"
    assert found["issues_cursor"] == LATEST["issues"]
    assert found["checkpoint"] == LATEST
    assert found["valid"] == {"v4": 3, "v3": 1}
    assert [(row["issue_id"], row["key"]) for row in found["invalid"]] == [
        ("issue-0921", "ai_wiki_incremental_checkpoint_v4")
    ]
    assert "sha must be a 40-hex commit" in found["invalid"][0]["error"]
    assert (cache / "autopilot-runs" / "offset-000000.json").exists()
    assert (cache / "issue-metadata" / "issue-0919.json").exists()
    assert all(call[:3] != ["issue", "metadata", "set"] for call in multica.calls())


def test_find_falls_back_to_v3_then_reports_not_found(multica: FakeMultica) -> None:
    state = production_like_state()
    state["metadata"] = {"issue-0902": state["metadata"]["issue-0902"]}
    multica.save(state)

    fallback = script("checkpoint.py", "find", "--autopilot", AUTOPILOT)
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    found = json.loads(fallback.stdout)
    assert (found["version"], found["fallback"], found["key"]) == (3, True, "ai_wiki_incremental_checkpoint_v3")

    multica.save({**state, "metadata": {}})
    missing = script("checkpoint.py", "find", "--autopilot", AUTOPILOT)
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["found"] is False

    seeded = script("checkpoint.py", "find", "--autopilot", AUTOPILOT, "--seed-issue", "issue-seed")
    assert seeded.returncode == 1
    assert ["issue", "metadata", "list", "issue-seed", "--output", "json"] in multica.calls()


def test_find_pages_runs_and_reports_unreadable_issues(multica: FakeMultica) -> None:
    state = production_like_state()
    newest = state["runs"][:3]
    filler = [{"issue_id": "issue-filler", "status": "failed"}] * 100
    state["runs"] = newest + filler + state["runs"][3:]
    state["fail"] = {"issue metadata list issue-0917": "Error: request failed: 502 Bad Gateway"}
    multica.save(state)

    result = script("checkpoint.py", "find", "--autopilot", AUTOPILOT)

    assert result.returncode == 3, result.stdout + result.stderr
    found = json.loads(result.stdout)
    assert found["runs"] == 108
    assert found["issue_id"] == "issue-0919"
    assert found["valid"] == {"v4": 2, "v3": 1}
    assert found["unreadable"] == [{"issue_id": "issue-0917", "error": (
        "multica issue metadata list exited 1: Error: request failed: 502 Bad Gateway")}]
    offsets = [call[call.index("--offset") + 1] for call in multica.calls() if call[:2] == ["autopilot", "runs"]]
    assert offsets == ["0", "100"]


def test_find_can_exclude_a_permanently_unreadable_run_issue(multica: FakeMultica) -> None:
    state = production_like_state()
    state["fail"] = {"issue metadata list issue-0917": "Error: issue not found (404)"}
    multica.save(state)

    result = script("checkpoint.py", "find", "--autopilot", AUTOPILOT, "--exclude-issue", "issue-0917")

    assert result.returncode == 0, result.stdout + result.stderr
    found = json.loads(result.stdout)
    assert found["issue_id"] == "issue-0919" and found["unreadable"] == []
    assert found["excluded"] == ["issue-0917"]
    assert ["issue", "metadata", "list", "issue-0917", "--output", "json"] not in multica.calls()


@pytest.mark.parametrize(
    ("metadata", "fail"),
    [
        ({"issue-0919": {"ai_wiki_incremental_checkpoint_v4": stored(LATEST)}},
         {"issue metadata list": "Error: request failed: 502 Bad Gateway"}),
        ({"issue-0919": [], "issue-0902": {"ai_wiki_incremental_checkpoint_v3": stored(v3("2026-09-02T20:52:31Z"))}},
         {}),
        ({"issue-0902": {"ai_wiki_incremental_checkpoint_v3": stored(v3("2026-09-02T20:52:31Z"))}},
         {"issue metadata list issue-0919": "Error: request failed: 429 Too Many Requests"}),
    ],
    ids=["all-unreadable", "non-object-metadata-hides-v4", "unreadable-before-v3-fallback"],
)
def test_find_fails_closed_when_an_unread_issue_may_hold_the_checkpoint(
    multica: FakeMultica, metadata: dict[str, Any], fail: dict[str, str],
) -> None:
    # A Multica outage used to look exactly like a first run (exit 1, "no checkpoint"): the
    # caller would bootstrap every repo as new, or fall back to a v3 missing newer repos.
    multica.save({**production_like_state(), "metadata": metadata, "fail": fail})

    result = script("checkpoint.py", "find", "--autopilot", AUTOPILOT)

    assert result.returncode == 2, result.stdout + result.stderr
    found = json.loads(result.stdout)
    assert found["found"] is False
    assert found["unreadable"]
    assert "checkpoint" not in found


def scan_report(candidate_repos: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "checkpoint_candidate": {"version": 4, "repo_root": LATEST["repo_root"], "repos": candidate_repos,
                                 "issues": LATEST["issues"]},
        "repos": extra.pop("repos", []),
        "warnings": extra.pop("warnings", []),
        **extra,
    }


def test_build_merges_candidate_cursor_and_completion_into_valid_v4(tmp_path: Path) -> None:
    web_id, web = repo_row("web-server", "d")
    public_id, public = repo_row("public-server", "b", "main", stale_since="2026-09-23T20:01:00Z",
                                 last_error="timeout")
    seo_id, seo = repo_row("seo", "e", "dev")
    rows = {web_id: web, public_id: public, seo_id: seo}
    scan = tmp_path / "repo-scan.json"
    scan.write_text(json.dumps(scan_report(
        rows,
        repos=[{"repo_id": web_id, "name": "web-server", "state": "changed", "previous_sha": "a" * 40},
               {"repo_id": public_id, "name": "public-server", "state": "failed", "previous_sha": "b" * 40},
               {"repo_id": seo_id, "name": "seo", "state": "rebaselined", "baseline_required": True,
                "rebaseline_reason": "branch_override", "previous_sha": None}],
        warnings=[{"type": "default_branch_drift"}, {"type": "default_branch_drift"}, {"type": "truncated"}],
    )), encoding="utf-8")
    delta = tmp_path / "issue-delta.json"
    delta.write_text(json.dumps({"cursor": LATEST["issues"],
                                 "next_cursor": {"updated_at": "2026-09-23T17:53:52.372956Z",
                                                 "id": "01a0cf47-8de6-7247-9c68-43a5d759b53e"}}), encoding="utf-8")
    previous = tmp_path / "find.json"
    previous.write_text(json.dumps({"found": True, "checkpoint": LATEST}), encoding="utf-8")
    output = tmp_path / "v4.json"
    args = ["build", "--scan", str(scan), "--issues-cursor", str(delta), "--previous", str(previous),
            "--completed-at", "2026-09-24T04:30:00Z", "--output", str(output)]

    undecided = script("checkpoint.py", *args)
    assert undecided.returncode == 2
    assert "baseline_required repos need --baseline-done or --baseline-waive REPO=REASON: seo" in undecided.stderr
    assert not output.exists()

    result = script("checkpoint.py", *args, "--baseline-waive", "seo=dead release branch; durable docs only")

    assert result.returncode == 0, result.stdout + result.stderr
    built = json.loads(output.read_text(encoding="utf-8"))
    baseline = {"sha": "e" * 40, "at": "2026-09-24T04:30:00Z", "disposition": "waived",
                "reason": "dead release branch; durable docs only"}
    assert built == {
        "version": 4, "repo_root": LATEST["repo_root"], "repos": {**rows, seo_id: {**seo, "baseline": baseline}},
        "issues": {"updated_at": "2026-09-23T17:53:52.372956Z", "id": "01a0cf47-8de6-7247-9c68-43a5d759b53e"},
        "completed_at": "2026-09-24T04:30:00Z",
    }
    summary = json.loads(result.stdout)
    assert summary["issues_cursor"]["old"] == LATEST["issues"]
    assert summary["stale_repos"] == ["public-server"]
    assert summary["baseline_required"] == [{
        "name": "seo", "repo_id": seo_id, "state": "rebaselined", "reason": "branch_override",
        "baseline": {"disposition": "waived", "reason": "dead release branch; durable docs only"},
    }]
    assert summary["warnings"] == {"default_branch_drift": 2, "truncated": 1}

    done = script("checkpoint.py", *args, "--baseline-done", seo_id)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["repos"][seo_id]["baseline"] == {
        "sha": "e" * 40, "at": "2026-09-24T04:30:00Z", "disposition": "done",
    }
    unknown = script("checkpoint.py", *args, "--baseline-done", "seo", "--baseline-done", "web-server")
    assert unknown.returncode == 2
    assert "'web-server' matches 0 baseline_required repos" in unknown.stderr

    unchanged = script("checkpoint.py", "build", "--scan", str(scan), "--completed-at", "2026-09-24T04:30:00Z",
                       "--baseline-done", "seo", "--output", str(tmp_path / "unchanged.json"))
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert json.loads(unchanged.stdout)["issues_cursor"]["source"] == "unchanged"
    assert json.loads((tmp_path / "unchanged.json").read_text(encoding="utf-8"))["issues"] == LATEST["issues"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda repos, args: repos.pop(next(iter(repos))), "repos missing from the new checkpoint"),
        (lambda repos, args: args.extend(["--issues-updated-at", "2026-09-18T00:00:00Z", "--issues-id", "z"]),
         "issues cursor would move backwards"),
        (lambda repos, args: args.__setitem__(1, "2026-09-20T10:35:37Z"), "completed_at must be later"),
        (lambda repos, args: repos.update({"wrong-key": repo_row("seo", "e")[1]}), "key must be repo_id(remote_url)"),
        (lambda repos, args: next(iter(repos.values())).update(sha="abc"), "sha must be a 40-hex commit"),
        # A scan made without the checkpoint (every repo new) is not a delta from --previous.
        (lambda repos, args: next(iter(repos.values())).update(sha="f" * 40),
         "scan was not computed against --previous: web-server"),
        # Production smoke run: a delta from a hand-picked later cursor skips 09-19..09-23.
        (lambda repos, args: args.extend(["--issues-cursor", "DELTA:2026-09-23T00:00:00Z"]),
         "issue delta starts after the previous issues cursor"),
        (lambda repos, args: args.append("BARE"), "is not a scanner report"),
    ],
    ids=["drops-repo", "cursor-backwards", "not-later", "wrong-key", "short-sha", "scan-not-from-previous",
         "delta-starts-late", "bare-candidate"],
)
def test_build_refuses_lossy_or_malformed_checkpoints(tmp_path: Path, mutate: Any, message: str) -> None:
    repos = json.loads(json.dumps(LATEST["repos"]))
    args = ["--completed-at", "2026-09-24T04:30:00Z"]
    mutate(repos, args)
    scan = tmp_path / "repo-scan.json"
    report = scan_report(repos)
    if args[-1] == "BARE":
        args.pop()
        report = report["checkpoint_candidate"]
    scan.write_text(json.dumps(report), encoding="utf-8")
    for index, value in enumerate(args):
        if value.startswith("DELTA:"):
            delta = tmp_path / "issue-delta.json"
            delta.write_text(json.dumps({"cursor": {"updated_at": value.removeprefix("DELTA:"), "id": "a"},
                                         "next_cursor": {"updated_at": "2026-09-23T17:53:52Z", "id": "b"}}),
                             encoding="utf-8")
            args[index] = str(delta)
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"metadata": {"ai_wiki_incremental_checkpoint_v4": stored(LATEST)}}),
                        encoding="utf-8")
    output = tmp_path / "v4.json"

    result = script("checkpoint.py", "build", "--scan", str(scan), "--previous", str(previous),
                    "--output", str(output), *args)

    assert result.returncode == 2
    assert message in result.stderr
    assert not output.exists()


def test_find_output_flows_through_scanner_and_build(tmp_path: Path) -> None:
    work, remote = tmp_path / "work", tmp_path / "remote.git"
    work.mkdir()
    for command in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *command], cwd=work, check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)
    root = tmp_path / "reference"
    root.mkdir()
    first_scan = tmp_path / "first-scan.json"
    scanned = script("scan_reference_repos.py", "--root", str(root), "--required-remote", str(remote),
                     "--cache-dir", str(tmp_path / "cache"), "--output", str(first_scan), "--quiet")
    assert scanned.returncode == 0, scanned.stderr
    first_v4 = tmp_path / "first-v4.json"

    built = script("checkpoint.py", "build", "--scan", str(first_scan), "--issues-updated-at",
                   "2026-09-23T17:53:52Z", "--issues-id", "01a0cf47", "--completed-at", "2026-09-23T18:00:00Z",
                   "--baseline-done", "remote", "--output", str(first_v4))

    assert built.returncode == 0, built.stdout + built.stderr
    summary = json.loads(built.stdout)["baseline_required"]
    assert [(row["name"], row["state"], row["baseline"]["disposition"]) for row in summary] == [
        ("remote", "new", "done")
    ]

    # The next run feeds `find` output (the {found, checkpoint} envelope) to both tools.
    found = tmp_path / "find.json"
    found.write_text(json.dumps({"found": True, "checkpoint": json.loads(first_v4.read_text(encoding="utf-8"))}),
                     encoding="utf-8")
    next_scan = tmp_path / "next-scan.json"
    rescanned = script("scan_reference_repos.py", "--root", str(root), "--required-remote", str(remote),
                       "--checkpoint-json", str(found), "--cache-dir", str(tmp_path / "cache"),
                       "--output", str(next_scan), "--quiet")
    assert rescanned.returncode == 0, rescanned.stderr
    assert json.loads(next_scan.read_text(encoding="utf-8"))["repos"][0]["state"] == "unchanged"
    args = ["build", "--previous", str(found), "--completed-at", "2026-09-24T18:00:00Z",
            "--output", str(tmp_path / "next-v4.json")]
    assert script("checkpoint.py", *args, "--scan", str(next_scan)).returncode == 0

    stale = script("checkpoint.py", *args, "--scan", str(first_scan), "--baseline-done", "remote",
                   "--issues-updated-at", "2026-09-24T17:00:00Z", "--issues-id", "01a0cf48")
    assert stale.returncode == 2
    assert "scan was not computed against --previous: remote" in stale.stderr


def test_write_sets_string_value_and_verifies_readback(multica: FakeMultica, tmp_path: Path) -> None:
    multica.save({"metadata": {}})
    checkpoint = tmp_path / "v4.json"
    checkpoint.write_text(json.dumps(LATEST), encoding="utf-8")

    result = script("checkpoint.py", "write", "--issue", "issue-0924", "--file", str(checkpoint))

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["verified"] is True
    value = multica.load()["metadata"]["issue-0924"]["ai_wiki_incremental_checkpoint_v4"]
    assert isinstance(value, str) and json.loads(value) == LATEST
    commands = [call[:3] for call in multica.calls()]
    assert commands == [["issue", "metadata", "set"], ["issue", "metadata", "get"]]
    assert multica.calls()[0][-4:] == ["--type", "string", "--output", "json"]

    tampered = {**LATEST, "completed_at": "2026-09-21T00:00:00Z"}
    multica.save({"metadata": {}, "readback": {"issue-0924": stored(tampered)}})
    mismatch = script("checkpoint.py", "write", "--issue", "issue-0924", "--file", str(checkpoint))
    assert mismatch.returncode == 2
    assert "does not match" in mismatch.stderr


def test_write_refuses_invalid_checkpoint_without_calling_multica(multica: FakeMultica, tmp_path: Path) -> None:
    checkpoint = tmp_path / "v4.json"
    checkpoint.write_text(json.dumps({**LATEST, "issues": None}), encoding="utf-8")

    result = script("checkpoint.py", "write", "--issue", "issue-0924", "--file", str(checkpoint))

    assert result.returncode == 2
    assert "issues must be an object" in result.stderr
    assert multica.calls() == []


CURSOR = {"updated_at": "2026-09-19T16:40:04Z", "id": "01a0a49d-579b-7379-9d1a-ada7254035f8"}


def issue(issue_id: str, created: str, updated: str, activity: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": issue_id, "identifier": f"WAIO-{issue_id}", "title": f"title {issue_id}", "status": "in_review",
            "description": extra.pop("description", "d"), "created_at": created, "updated_at": updated,
            "last_activity_at": activity, "metadata": extra.pop("metadata", {}), "parent_issue_id": None,
            "assignee_type": "agent", **extra}


def delta_state() -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "runs": [{"issue_id": "run", "status": "failed"}],
        "issues": [
            issue("old", "2026-07-20T09:00:00Z", "2026-07-20T11:11:27Z"),  # last_activity_at null
            # updated_at before the cursor; a later comment only moves last_activity_at.
            issue("comment-only", "2026-09-10T00:00:00Z", "2026-09-17T02:19:54Z", "2026-09-20T05:28:16.979729Z"),
            issue("new", "2026-09-21T13:44:04Z", "2026-09-21T14:38:45Z", "2026-09-21T14:38:45.891006Z",
                  description="x" * 50, metadata={"pr_url": "https://example.com/pr/1"}),
            # Same timestamp as the cursor: the (changed_at, id) tuple decides.
            issue("01a0a49c-tie-before", "2026-09-01T00:00:00Z", "2026-09-19T16:40:04Z"),
            issue("01a0a49e-tie-after", "2026-09-01T00:00:01Z", "2026-09-19T16:40:04Z"),
            issue("run", "2026-09-22T20:00:31Z", "2026-09-22T20:04:46Z"),
            issue("current", "2026-09-23T10:00:31Z", "2026-09-23T10:00:40Z"),
            issue("receipt", "2026-09-10T20:00:00Z", "2026-09-21T18:57:43Z",
                  metadata={"ai_wiki_recovery_receipt": "{}"}),
            issue("explicit", "2026-09-05T00:00:00Z", "2026-09-22T00:00:00Z"),
            issue("just-now", "2026-09-22T00:00:00Z", now, now),
        ],
        "comments": {
            "comment-only": [
                {"id": "c-old", "author_type": "member", "content": "before cursor",
                 "created_at": "2026-09-18T00:00:00Z"},
                {"id": "c-new", "author_type": "member", "content": "decision: keep master", "parent_id": "p",
                 "created_at": "2026-09-20T05:28:16Z"},
            ],
            "new": [{"id": f"n{index}", "author_type": "agent", "content": "y" * 40,
                     "created_at": f"2026-09-21T14:0{index}:00Z"} for index in range(4)],
        },
    }


def test_issue_delta_lists_changed_issues_and_excludes_maintenance(
    multica: FakeMultica, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    multica.save(delta_state())
    monkeypatch.setenv("MULTICA_ISSUE_ID", "current")
    cursor = tmp_path / "find.json"
    cursor.write_text(json.dumps({"found": True, "checkpoint": {**LATEST, "issues": CURSOR}}), encoding="utf-8")
    cache = tmp_path / "cache"

    result = script("issue_delta.py", "--autopilot", AUTOPILOT, "--cursor-json", str(cursor),
                    "--exclude-issue", "explicit", "--cache-dir", str(cache), "--page-size", "3",
                    "--max-chars", "20", "--max-comments", "2")

    assert result.returncode == 0, result.stdout + result.stderr
    delta = json.loads(result.stdout)
    assert [row["id"] for row in delta["candidates"]] == ["01a0a49e-tie-after", "comment-only", "new", "just-now"]
    assert [row["deferred"] for row in delta["candidates"]] == [False, False, False, True]
    assert delta["counts"] == {
        "listed": 10, "changed": 8, "candidates": 4, "excluded": 4, "deferred": 1, "comments": 5,
        "excluded_by_reason": {"autopilot_run": 1, "current_issue": 1, "explicit": 1, "maintenance_metadata": 1},
    }
    # The deferred issue does not move the cursor, so the next run lists it again.
    assert delta["next_cursor"] == {"updated_at": "2026-09-23T10:00:40Z", "id": "current"}
    comment_only = delta["candidates"][1]
    assert comment_only["new"] is False
    assert comment_only["changed_at"] == "2026-09-20T05:28:16.979729Z"
    assert [row["id"] for row in comment_only["comments"]] == ["c-new"]
    assert comment_only["comments"][0]["excerpt"] == "decision: keep maste…"
    new = delta["candidates"][2]
    assert new["new"] is True
    assert (new["comments_in_window"], new["comments_omitted"]) == (4, 2)
    assert [row["id"] for row in new["comments"]] == ["n2", "n3"]
    assert (new["description_chars"], new["description_excerpt"]) == (50, "x" * 20 + "…")
    assert json.loads(Path(new["comments_file"]).read_text(encoding="utf-8"))[0]["id"] == "n0"

    calls = multica.calls()
    commented = sorted(call[3] for call in calls if call[:3] == ["issue", "comment", "list"])
    assert commented == ["01a0a49e-tie-after", "comment-only", "just-now", "new"]
    assert all(call[call.index("--since") + 1] == "2026-09-19T16:40:04Z"
               for call in calls if call[:3] == ["issue", "comment", "list"])
    assert all(call[:2] in (["autopilot", "runs"], ["issue", "list"]) or call[:3] == ["issue", "comment", "list"]
               for call in calls), "issue_delta must only read"
    # Pages overlap by one row so a shifted listing is detected.
    assert sorted(path.name for path in (cache / "issues").iterdir()) == [
        "offset-000000.json", "offset-000002.json", "offset-000004.json", "offset-000006.json",
        "offset-000008.json",
    ]


def test_issue_delta_reports_deferred_issue_comments_before_the_cursor_passes_them(
    multica: FakeMultica, tmp_path: Path,
) -> None:
    # A decision comment on 09-20 on an issue touched again inside the settle window was
    # skipped by run 1 (deferred) and by run 2 (comments fetched only after next_cursor).
    # 30s ago: inside run 1's 120s settle window, settled for run 2 (--settle-seconds 0).
    now = (datetime.now(UTC) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    multica.save({
        "runs": [],
        "issues": [issue("active", "2026-09-01T00:00:00Z", now, now),
                   issue("other", "2026-09-02T00:00:00Z", "2026-09-21T00:00:00Z")],
        "comments": {"active": [
            {"id": "c-decision", "author_type": "member", "content": "DECISION: pin release to master",
             "created_at": "2026-09-20T00:00:00Z"},
            {"id": "c-late", "author_type": "member", "content": "later note", "created_at": now},
        ]},
    })
    first_output = tmp_path / "delta-1.json"

    first = script("issue_delta.py", "--autopilot", AUTOPILOT, "--since-updated-at", CURSOR["updated_at"],
                   "--since-id", CURSOR["id"], "--cache-dir", str(tmp_path / "c1"), "--output", str(first_output))

    assert first.returncode == 0, first.stdout + first.stderr
    run1 = json.loads(first.stdout)
    assert run1["next_cursor"] == {"updated_at": "2026-09-21T00:00:00Z", "id": "other"}
    active = next(row for row in run1["candidates"] if row["id"] == "active")
    assert active["deferred"] is True
    assert [row["id"] for row in active["comments"]] == ["c-decision"]

    second = script("issue_delta.py", "--autopilot", AUTOPILOT, "--cursor-json", str(first_output),
                    "--cache-dir", str(tmp_path / "c2"), "--settle-seconds", "0")

    assert second.returncode == 0, second.stdout + second.stderr
    run2 = json.loads(second.stdout)
    assert [row["id"] for row in run2["candidates"]] == ["active"]
    assert [row["id"] for row in run2["candidates"][0]["comments"]] == ["c-late"]


def test_issue_delta_pages_by_returned_rows_when_the_server_caps_the_limit(
    multica: FakeMultica, tmp_path: Path,
) -> None:
    # Production caps issue list at 100 rows but echoes --limit 200 with has_more: stepping
    # the offset by the requested size listed 300 of 594 issues and still exited 0.
    issues = [issue(f"i{index:02d}", f"2026-09-{10 + index:02d}T00:00:00Z", f"2026-09-{10 + index:02d}T01:00:00Z")
              for index in range(10)]
    multica.save({"runs": [], "issues": issues, "page_cap": 3})

    result = script("issue_delta.py", "--autopilot", AUTOPILOT, "--since-updated-at", "2026-09-01T00:00:00Z",
                    "--since-id", "x", "--cache-dir", str(tmp_path / "cache"), "--page-size", "5")

    assert result.returncode == 0, result.stdout + result.stderr
    delta = json.loads(result.stdout)
    assert delta["counts"]["listed"] == 10
    assert [row["id"] for row in delta["candidates"]] == [row["id"] for row in issues]
    offsets = [call[call.index("--offset") + 1] for call in multica.calls() if call[:2] == ["issue", "list"]]
    assert offsets == ["0", "2", "4", "6", "8"]


@pytest.mark.parametrize(
    ("orders", "message"),
    [
        # i01 deleted after the first page: later rows shift left and t1 would be skipped,
        # while the unique count still equals the (smaller) total.
        ({"2": ["i00", "t0", "t1", "t2", "t3", "t4"]}, "issue list shifted at offset 2"),
        # An unstable order among created_at ties (WAIO-406..410 share 2026-09-08T16:59:44Z)
        # keeps each overlap row but never returns t3.
        ({"2": ["i00", "i01", "t0", "t2", "t1", "t3", "t4"], "4": ["i00", "i01", "t0", "t3", "t1", "t2", "t4"]},
         "issue list returned 6 unique issues but total is 7"),
    ],
    ids=["deleted-mid-listing", "unstable-ties"],
)
def test_issue_delta_fails_closed_when_the_listing_is_not_provably_complete(
    multica: FakeMultica, tmp_path: Path, orders: dict[str, list[str]], message: str,
) -> None:
    rows = [issue("i00", "2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z"),
            issue("i01", "2026-09-02T00:00:00Z", "2026-09-22T00:00:00Z")]
    rows += [issue(f"t{index}", "2026-09-08T16:59:44Z", "2026-09-22T00:00:00Z") for index in range(5)]
    multica.save({"runs": [], "issues": rows, "order_by_offset": orders})
    output = tmp_path / "delta.json"

    result = script("issue_delta.py", "--autopilot", AUTOPILOT, "--since-updated-at", "2026-09-01T00:00:00Z",
                    "--since-id", "x", "--cache-dir", str(tmp_path / "cache"), "--page-size", "3",
                    "--output", str(output))

    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
    assert not output.exists()


def test_issue_delta_without_changes_keeps_cursor_and_fails_closed(multica: FakeMultica, tmp_path: Path) -> None:
    state = delta_state()
    state["issues"] = state["issues"][:1]
    multica.save(state)
    args = ["--autopilot", AUTOPILOT, "--since-updated-at", CURSOR["updated_at"], "--since-id", CURSOR["id"],
            "--cache-dir", str(tmp_path / "cache")]

    quiet = script("issue_delta.py", *args)
    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    assert json.loads(quiet.stdout)["next_cursor"] == CURSOR

    state = delta_state()
    state["fail"] = {"issue comment list new": "Error: request failed: 503 Service Unavailable"}
    multica.save(state)
    output = tmp_path / "delta.json"
    failed = script("issue_delta.py", *args, "--output", str(output))
    assert failed.returncode == 2
    assert "503 Service Unavailable" in failed.stderr
    assert not output.exists()
