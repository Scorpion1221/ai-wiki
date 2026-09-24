"""The maintainer loop end to end (design §4, §10.4; acceptance §13 W9).

A temporary bundle behind a bare remote is served by the real gate through a TestClient;
the collectors scan a real reference repository and the fake ``multica``. A deterministic
fake agent drives ``maint begin/next/add-evidence/skip/park/split/end`` and ``propose``
through the CLI in-process, choosing each item's fate by its topic, as the Maintainer's
triage would. The run's report is compared with a golden file after its ids, commit shas
and times are replaced by stable placeholders.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from gate_fixture import CURATOR, METRIC, Gate, git

from aiwiki import version
from aiwiki.cli import main as cli
from aiwiki.cli import maint, workspace
from aiwiki.maint import collect_repos
from aiwiki.runtime import failure

FAKE = Path(__file__).with_name("fake_multica.py")
GOLDEN = Path(__file__).parent / "fixtures" / "maint_loop_report.md"
RUN = "WAIO-9"
T0 = "2026-01-01T00:00:00Z"
PROBE = "metrics/probe-retry-cap.md"


@pytest.fixture
def loop(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    gate.calls = gate.connect()
    monkeypatch.setattr(workspace, "RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(workspace, "POLL_S", 0.02)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, script in (("multica", f'exec "{sys.executable}" "{FAKE}" "$@"'), ("uv", "exit 0")):
        (tools / name).write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MULTICA_STATE", str(tmp_path / "multica.json"))
    monkeypatch.delenv("MULTICA_ISSUE_ID", raising=False)
    multica({"runs": [], "issues": [issue()], "comments": {"issue-7": [
        {"id": "c-1", "author_id": "m-1", "author_type": "member", "type": "comment",
         "created_at": "2026-01-02T00:00:00Z", "content": "We decided to keep the retry cap at 3."},
        {"id": "c-2", "author_id": "agent-maintainer", "author_type": "agent", "type": "comment",
         "created_at": "2026-01-02T00:05:00Z", "content": "maintenance report"}]}}, tmp_path)
    reference = tmp_path / "reference"
    reference.mkdir()
    remote, work, first = control_repo(tmp_path)
    (reference / "control").symlink_to(work, target_is_directory=True)
    config = tmp_path / "maint.json"
    config.write_text(json.dumps({"repos": {"root": str(reference)},
                                  "issues": {"autopilot": "ap-maint", "exclude_agents": ["agent-maintainer"]}}),
                      encoding="utf-8")
    yield {"gate": gate, "tmp": tmp_path, "remote": remote, "work": work, "first": first, "config": config,
           "st": tmp_path / "st", "identity": collect_repos.canonical_remote(str(remote))}
    gate.close()


def multica(state: dict, tmp_path: Path) -> None:
    (tmp_path / "multica.json").write_text(json.dumps(state), encoding="utf-8")


def issue() -> dict:
    return {"id": "issue-7", "identifier": "WAIO-7", "title": "Retry cap", "status": "done",
            "description": "Should retries be capped?", "created_at": "2026-01-01T12:00:00Z",
            "updated_at": "2026-01-02T00:05:00Z", "last_activity_at": "2026-01-02T00:05:00Z", "metadata": {},
            "assignee_type": "member", "assignee_id": "m-1"}


def commit(work: Path, message: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", message)
    git(work, "push", "-q", "origin", "HEAD:main")
    return git(work, "rev-parse", "HEAD")


def control_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "control.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "control-work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "t@local")
    git(work, "config", "user.name", "t")
    first = commit(work, "initial", {
        "README.md": "control\n", "tasks/funnel/README.md": "# Funnel task\n\nOwner: web team\n",
        "memory/learnings.md": "- first lesson\n", "docs/a.md": "# A\n", "docs/b.md": "# B\n",
        "src/app.ts": "export {}\n"})
    return remote, work, first


def wiki(capsys, *args) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args])
    except SystemExit as exit_:
        code = exit_.code
    return code, capsys.readouterr().out


def wiki_json(capsys, *args) -> tuple[int, dict]:
    """The command's JSON, after whatever the in-process writer printed while it ran."""
    code, out = wiki(capsys, *args, "--json")
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


def v4(loop: dict, sha: str) -> Path:
    """A P0 v4 checkpoint naming the control repository at ``sha`` and the issues cursor at T0."""
    path = loop["tmp"] / "v4.json"
    path.write_text(json.dumps({"checkpoint": {
        "version": 4, "repo_root": str(loop["tmp"] / "reference"), "completed_at": T0,
        "issues": {"updated_at": T0, "id": "0"},
        "repos": {collect_repos.repo_id(loop["identity"]): {
            "name": "control", "remote_url": str(loop["remote"]), "branch": "main", "sha": sha}}}}),
        encoding="utf-8")
    return path


def normalized(text: str, tmp_path: Path) -> str:
    """Ids, shas and times as placeholders numbered by first appearance; the temp dir as <tmp>."""
    for path in sorted({str(tmp_path.resolve()), str(tmp_path)}, key=len, reverse=True):
        text = text.replace(path, "<tmp>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", "<time>", text)
    names: dict[str, str] = {}
    return re.sub(r"(?<![0-9A-Za-z])[0-9a-f]{7,64}(?![0-9A-Za-z])",
                  lambda match: names.setdefault(match[0], f"<h{len(names) + 1}>"), text)


def item(loop: dict, item_id: str) -> dict:
    response = loop["gate"].client.get(f"/maint/items/{item_id}", params={"bundle": "kb-a"},
                                       headers=loop["gate"].headers())
    assert response.status_code == 200, response.text
    return response.json()


def maint_file(loop: dict, *parts: str) -> Path:
    return loop["gate"].root / "kb-a" / ".okf" / "maint" / Path(*parts)


def edit(path: Path, **changes) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    for key, change in changes.items():
        value[key] = change(value[key]) if callable(change) else change
    path.write_text(json.dumps(value), encoding="utf-8")


def expire_lease(loop: dict) -> None:
    """The run died holding the lease (the Multica task timed out): its TTL has passed."""
    edit(maint_file(loop, "lease-maintainer.json"), renewed_at="2020-01-01T00:00:00Z",
         expires_at="2020-01-01T03:00:00Z")


def enqueue(loop: dict, topic: str, text: str) -> str:
    gate = loop["gate"]
    response = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={
        "items": [{"origin": {"kind": "repo"}, "topic_key": topic, "brief": topic, "files": [
            {"name": "notes.md", "content_b64": base64.b64encode(text.encode()).decode(),
             "origin": {"kind": "git-file"}}]}]})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


# --- the fake agent -------------------------------------------------------------------------------


def curate(capsys, loop: dict, brief: dict) -> None:
    ws, st, item_id = brief["commands"][0].split()[3], loop["st"], brief["item"]
    code, out = wiki(capsys, "concept", "new", PROBE, "--dir", ws, "--type", "Metric", "--title", "Probe retry cap",
                     "--description", "How many retries a funnel step gets", "--tags", "metric,retries",
                     "--source-id", "control-memory")
    assert code == 0, out
    with (Path(ws) / PROBE).open("a", encoding="utf-8") as concept:
        concept.write("Retries need a cap.[^control-memory]\n\n[^control-memory]: control memory\n")
    assert wiki_json(capsys, "maint", "next", "--state-dir", st)[0] == 12  # the edit must be proposed first
    code, verdict = wiki_json(capsys, "validate", "--dir", ws, "--item", item_id)
    assert code == 0, verdict
    code, receipt = wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", st)
    assert code == 0 and receipt["status"] == "done", receipt
    loop["changeset"] = receipt["id"]


def broken(capsys, loop: dict, brief: dict) -> None:
    """A concept the local gate refuses twice, so the item is parked as model_output."""
    ws, st, item_id = brief["commands"][0].split()[3], loop["st"], brief["item"]
    (Path(ws) / "metrics" / "probe-broken.md").write_text("---\ntype: Metric\ntitle: Broken: yes: no\n---\nbody\n",
                                                           encoding="utf-8")
    assert wiki_json(capsys, "validate", "--dir", ws, "--item", item_id)[0] == 6
    code, verdict = wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", st)
    assert code == 6 and "yaml_parse" in {error["code"] for error in verdict["errors"]}, verdict
    code, parked = wiki_json(capsys, "maint", "park", item_id, "--class", "model_output", "--detail",
                             "yaml_parse twice", "--state-dir", st)
    assert code == 0 and parked["reverted"] == ["metrics/probe-broken.md"], parked
    assert not (Path(ws) / "metrics" / "probe-broken.md").exists()


def agent(capsys, loop: dict) -> list[int]:
    """Serial triage until the queue is empty or the budget is spent; returns the last exit."""
    st, seen = loop["st"], {}
    while True:
        code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
        if code in (10, 11):
            return code, seen
        assert code == 0, brief
        assert len(json.dumps(brief).encode()) <= 2048
        topic, item_id = brief["topic_key"], brief["item"]
        seen[topic] = item_id
        if topic.endswith("#memory/learnings.md"):
            curate(capsys, loop, brief)
        elif topic.endswith("#tasks/funnel"):
            ref = f"{loop['remote']}@{loop['head'][:10]}:tasks/funnel/README.md#L1-1"
            code, added = wiki_json(capsys, "maint", "add-evidence", item_id, ref, "--state-dir", st)
            assert code == 0 and Path(added["path"]).read_text(encoding="utf-8") == "# Funnel task\n", added
            code, _ = wiki_json(capsys, "maint", "park", item_id, "--class", "context", "--detail",
                                "ran out of context", "--state-dir", st)
            assert code == 0
        elif topic.startswith("issue:"):
            code, skipped = wiki_json(capsys, "maint", "skip", item_id, "--reason", "no_durable_knowledge",
                                      "--state-dir", st)
            assert code == 0 and skipped["status"] == "skipped"
        elif topic.endswith("#docs"):
            names = [file["name"] for file in brief["files"]]
            groups = [",".join(name for name in names if not name.endswith("-b.md")),
                      next(name for name in names if name.endswith("-b.md"))]
            code, split = wiki_json(capsys, "maint", "split", item_id, "--group", groups[0], "--group", groups[1],
                                    "--state-dir", st)
            assert code == 0 and len(split["children"]) == 2
        elif topic.endswith("#src"):
            broken(capsys, loop, brief)
        else:
            assert "#split-" in topic
            code, _ = wiki_json(capsys, "maint", "skip", item_id, "--reason", f"duplicate_of:{PROBE}",
                                "--state-dir", st)
            assert code == 0


# --- the loop -------------------------------------------------------------------------------------


def test_a_run_collects_triages_proposes_and_reports(loop, capsys) -> None:
    gate, st = loop["gate"], loop["st"]
    code, imported = wiki_json(capsys, "maint", "import-v4", v4(loop, loop["first"]))
    assert code == 0 and [row["outcome"] for row in imported["cursors"]] == ["created", "created"]
    loop["head"] = commit(loop["work"], "work", {
        "memory/learnings.md": "- first lesson\n- retries need a cap\n", "tasks/funnel/status.md": "# Status\n\nDone\n",
        "docs/a.md": "# A\n\nnew\n", "docs/b.md": "# B\n\nnew\n", "src/app.ts": "export const cap = 3\n"})

    code, begun = wiki_json(capsys, "maint", "begin", "--run", RUN, "--max-items", 8, "--state-dir", st,
                            "--config", loop["config"])

    assert code == 0, begun
    assert len(json.dumps(begun).encode()) <= 2048  # the queue summary the agent reads
    assert begun["collect"]["repos"]["status"] == "ok" and begun["collect"]["repos"]["candidates"] == 4
    assert begun["collect"]["issues"]["status"] == "ok" and begun["collect"]["issues"]["candidates"] == 1
    assert begun["ready"] == {"issue": 1, "repo": 4}
    # The evidence is frozen, then the cursors move past it (design §4.3).
    repos = gate.client.get("/maint/cursors/repos", params={"bundle": "kb-a"}, headers=gate.headers()).json()
    assert repos["value"][loop["identity"]]["sha"] == loop["head"] and repos["run"] == RUN
    issues = gate.client.get("/maint/cursors/issues", params={"bundle": "kb-a"}, headers=gate.headers()).json()
    assert issues["value"]["updated_at"] > T0
    # A second begin of the same run renews its lease and collects nothing new.
    code, again = wiki_json(capsys, "maint", "begin", "--run", RUN, "--max-items", 8, "--state-dir", st,
                            "--config", loop["config"])
    assert code == 0 and again["collect"]["repos"]["candidates"] == 0 and again["budget"]["taken"] == 0

    code, seen = agent(capsys, loop)

    assert code == 10  # queue empty: the parked items wait for the next run
    status = {topic.rsplit("#", 1)[-1] if topic.startswith("repo:") else topic: item(loop, item_id)
              for topic, item_id in seen.items()}
    assert {topic: row["status"] for topic, row in status.items()} == {
        "memory/learnings.md": "curated", "tasks/funnel": "parked", "issue:WAIO-7": "skipped", "docs": "split",
        "src": "parked", "split-1": "duplicate", "split-2": "duplicate"}
    assert status["memory/learnings.md"]["resolution"]["job"] == loop["changeset"]
    assert status["src"]["attempts"]["counted"] == 1 and status["tasks/funnel"]["attempts"]["counted"] == 1
    assert sorted(status["docs"]["resolution"]["children"]) == sorted(
        [status["split-1"]["id"], status["split-2"]["id"]])
    assert any(file["name"].startswith("add-") for file in status["tasks/funnel"]["files"])
    # The commit names its changeset, principal, work item and run (design G14).
    message = git(gate.remote, "log", "-1", "--format=%B", "main")
    assert f"Changeset: {loop['changeset']}" in message and f"Principal: {CURATOR}" in message
    assert f"Work-Items: {status['memory/learnings.md']['id']}" in message and f"Run: {RUN}" in message
    assert PROBE in git(gate.remote, "show", "--name-only", "--format=", "main").splitlines()

    code, report = wiki_json(capsys, "maint", "end", "--run", RUN, "--state-dir", st)

    assert code == 0 and report["issue_status"] == "done", report
    assert report["queue"] | {"budget": None} == {
        "taken": 7, "curated": 1, "skipped": 1, "duplicate": 2, "parked": 2, "split": 1, "needs_human": 0,
        "in_progress": 0, "returned": 0, "remaining": 2, "stopped": "queue_empty", "budget": None}
    assert report["gate"] == {"submitted": 1, "items": 1, "first_pass": 1, "errors": {"yaml_parse": 1}}
    assert report["changesets"][0]["id"] == loop["changeset"]
    assert report["cursors"]["repos"]["value"] == repos["value"]  # the off-site copy admin cursor import reads
    assert report["lease"]["released"] is True
    if os.environ.get("AIWIKI_WRITE_GOLDEN"):
        GOLDEN.write_text(normalized(report["report"], loop["tmp"]), encoding="utf-8")
    assert normalized(report["report"], loop["tmp"]) == GOLDEN.read_text(encoding="utf-8")
    lease = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers()).json()["leases"]
    assert not (lease["maintainer"] or {}).get("active")
    assert not list((st / "runs").glob("current-*.json"))


def test_a_failed_collector_blocks_the_issue_but_not_the_loop(loop, capsys) -> None:
    st = loop["st"]
    # No issues cursor on the writer and none configured: the issues collector cannot start.
    code, begun = wiki_json(capsys, "maint", "begin", "--run", RUN, "--state-dir", st, "--config", loop["config"])

    assert code == 5, begun
    assert begun["collect"]["issues"]["status"] == "failed" and begun["collect"]["repos"]["status"] == "ok"
    code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and brief["topic_key"].startswith("rebaseline:")  # a new repository: one baseline item
    code, refused = wiki_json(capsys, "maint", "split", brief["item"], "--group", "baseline.md", "--state-dir", st)
    assert code == 2 and refused["status"] == 400  # a split needs two groups: the writer refuses the input
    code, _ = wiki_json(capsys, "maint", "skip", brief["item"], "--reason", "out_of_scope", "--state-dir", st)
    assert code == 0
    code, report = wiki_json(capsys, "maint", "end", "--run", RUN, "--state-dir", st)
    assert code == 0 and report["issue_status"] == "blocked" and report["blocked_by"] == ["issues"]
    # The next run collects the issues once they have a starting point; its report counts that.
    assert wiki_json(capsys, "maint", "begin", "--run", "WAIO-10", "--state-dir", st, "--config",
                     loop["config"])[0] == 5
    config = json.loads(loop["config"].read_text(encoding="utf-8"))
    config["issues"]["since"] = {"updated_at": T0, "id": "0"}
    loop["config"].write_text(json.dumps(config), encoding="utf-8")
    code, collected = wiki_json(capsys, "maint", "collect", "--only", "issues", "--state-dir", st,
                                "--config", loop["config"])
    assert code == 0 and collected["run"] == "WAIO-10" and collected["collect"]["issues"]["candidates"] == 1
    code, report = wiki_json(capsys, "maint", "end", "--run", "WAIO-10", "--state-dir", st)
    assert code == 0 and report["issue_status"] == "done" and report["collect"]["issues"]["created"] == 1


def test_the_budget_stops_the_run_and_a_preflight_failure_fails_closed(loop, capsys, monkeypatch) -> None:
    st = loop["st"]
    code, _ = wiki_json(capsys, "maint", "begin", "--run", RUN, "--max-items", 1, "--only", "repos",
                        "--state-dir", st, "--config", loop["config"])
    assert code == 0
    code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0
    code, resumed = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 11 and resumed["stopped"] == "budget"
    code, status = wiki_json(capsys, "maint", "status")
    assert code == 0 and status["leases"]["maintainer"]["run"] == RUN and status["items"]["in_progress"]["count"] == 1
    code, out = wiki(capsys, "maint", "status")
    assert code == 0 and "leases[2]" in out
    # Another run cannot begin while this one holds the lease.
    code, refused = wiki_json(capsys, "maint", "begin", "--run", "WAIO-10", "--only", "repos", "--state-dir",
                              loop["tmp"] / "st2", "--config", loop["config"])
    assert code == 4 and refused["failed"] == "lease" and refused["holder"]["run"] == RUN
    # The run's item goes back to the queue when the lease is released.
    code, report = wiki_json(capsys, "maint", "end", "--run", RUN, "--state-dir", st)
    assert code == 0 and report["queue"]["returned"] == 1 and report["items"][0]["detail"] == "interrupted 0/3"
    monkeypatch.setenv("PATH", "/nonexistent")  # no git, uv or multica: doctor fails closed
    code, failed = wiki_json(capsys, "maint", "begin", "--run", "WAIO-11", "--state-dir", st,
                             "--config", loop["config"])
    assert code == 4 and failed["failed"] == "doctor"
    assert {row["check"] for row in failed["checks"]} >= {"tool:git", "tool:multica"}


def test_add_evidence_extracts_only_what_the_collectors_may_cite(loop, capsys) -> None:
    st = loop["st"]
    run_issue = {**issue(), "id": "issue-612", "identifier": "WAIO-612", "description": "Retries are uncapped.",
                 "assignee_type": "agent", "assignee_id": "agent-maintainer"}  # text the Maintainer controls
    state = json.loads((loop["tmp"] / "multica.json").read_text(encoding="utf-8"))
    multica({**state, "issues": [issue(), run_issue]}, loop["tmp"])
    assert wiki_json(capsys, "maint", "begin", "--run", RUN, "--only", "repos", "--state-dir", st,
                     "--config", loop["config"])[0] == 0
    code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0
    item_id = brief["item"]

    code, added = wiki_json(capsys, "maint", "add-evidence", item_id, "issue:WAIO-7", "--state-dir", st,
                            "--config", loop["config"])

    assert code == 0, added
    text = Path(added["path"]).read_text(encoding="utf-8")
    assert "We decided to keep the retry cap at 3." in text and "maintenance report" not in text  # agent excluded
    refused = [
        "issue:WAIO-7#c-2",  # the Maintainer's own comment is never evidence
        "issue:WAIO-612",  # nor an issue assigned to it
        f"https://code.example/unknown.git@{loop['first'][:7]}:README.md",  # not a collected repository
        f"{loop['remote']}@{loop['first'][:7]}:../outside.md",
        f"{loop['remote']}@{loop['first'][:7]}:README.md#L5-9",  # README.md has one line
    ]
    for ref in refused:
        code, error = wiki_json(capsys, "maint", "add-evidence", item_id, ref, "--state-dir", st,
                                "--config", loop["config"])
        assert code == 2, (ref, error)
    code, error = wiki_json(capsys, "maint", "add-evidence", item_id, "issue:WAIO-7", "--state-dir", st,
                            "--config", loop["tmp"] / "none.json")
    assert code == 2 and "issues.exclude_agents" in error["error"]  # no exclusions configured: fail closed
    # Past argparse (after --), a remote that git would read as an option is still refused.
    code, out = wiki(capsys, "maint", "add-evidence", "--state-dir", st, item_id, "--",
                     f"--upload-pack=touch@{loop['remote']}@{loop['first'][:7]}:README.md")
    assert code == 2 and "evidence is <remote>@<commit>" in out
    files = [file["name"] for file in item(loop, item_id)["files"]]
    assert files[-1] == added["file"] and len(files) == 2


def test_add_evidence_reads_the_reference_checkout_before_the_network(loop, capsys) -> None:
    st, url = loop["st"], f"file://{loop['remote'].resolve()}"
    git(loop["work"], "remote", "set-url", "origin", url)  # an identity that outlives the remote itself
    assert wiki_json(capsys, "maint", "begin", "--run", RUN, "--only", "repos", "--state-dir", st,
                     "--config", loop["config"])[0] == 0
    code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0
    loop["remote"].rename(loop["remote"].with_name("offline.git"))  # the host cannot reach the remote

    code, added = wiki_json(capsys, "maint", "add-evidence", brief["item"], f"{url}@{loop['first'][:10]}:README.md",
                            "--state-dir", st, "--config", loop["config"])

    assert code == 0 and Path(added["path"]).read_text(encoding="utf-8") == "control\n", added


# --- recovery -------------------------------------------------------------------------------------


def begin(capsys, loop: dict, run: str) -> dict:
    code, begun = wiki_json(capsys, "maint", "begin", "--run", run, "--only", "repos", "--state-dir", loop["st"],
                            "--config", loop["config"])
    assert code == 0, begun
    return begun


def test_end_reports_the_items_a_release_or_a_sweep_caps(loop, capsys) -> None:
    st = loop["st"]
    begin(capsys, loop, "R-1")
    enqueue(loop, "repo:x#tasks/b", "# B\n")
    for path in maint_file(loop, "items").glob("*/item.json"):  # one more claim reaches the cap
        edit(path, attempts=lambda attempts: {**attempts, "started": 7})
    code, first = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0

    code, report = wiki_json(capsys, "maint", "end", "--run", "R-1", "--state-dir", st)

    # The release interrupts the claimed item at its cap: a new needs_human item, exit 3.
    assert code == 3 and report["needs_human"] == [
        {"id": first["item"], "topic_key": first["topic_key"], "reason": "attempt_cap"}], report
    assert f"needs human: {first['item']} " in report["report"]
    # A run that dies holding its item: the next begin's sweep caps it, and that run reports it.
    begin(capsys, loop, "R-2")
    code, second = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and second["item"] != first["item"]
    expire_lease(loop)
    assert begin(capsys, loop, "R-3")["swept"]["interrupted"] == 1
    code, report = wiki_json(capsys, "maint", "end", "--run", "R-3", "--state-dir", st)
    assert code == 3 and second["item"] in {row["id"] for row in report["needs_human"]}, report


def test_a_lost_answer_or_an_expired_lease_never_strands_the_workspace(loop, capsys, monkeypatch) -> None:
    st = loop["st"]
    ws = Path(begin(capsys, loop, "R-1")["workspace"])
    enqueue(loop, "repo:x#tasks/b", "# B\n")
    code, brief = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0
    draft = ws / "metrics" / "draft.md"
    draft.write_text("---\ntype: Metric\n---\nhalf done\n", encoding="utf-8")
    code, dirty = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 12 and dirty["item"] == brief["item"] and dirty["changes"] == ["metrics/draft.md"], dirty
    real = cli._http

    def lost(method, route, **kwargs):  # the writer parks the item, then the connection drops
        answer = real(method, route, **kwargs)
        if route.endswith("/resolve"):
            raise ConnectionResetError("connection reset by peer")
        return answer

    monkeypatch.setattr(cli, "_http", lost)
    park = ("maint", "park", brief["item"], "--class", "context", "--detail", "out of context", "--state-dir", st)
    assert wiki_json(capsys, *park)[0] == 1 and draft.exists()
    monkeypatch.setattr(cli, "_http", real)

    code, parked = wiki_json(capsys, *park)

    assert code == 0 and parked["status"] == "parked" and parked["reverted"] == ["metrics/draft.md"], parked
    assert item(loop, brief["item"])["attempts"]["counted"] == 1  # parked once, not twice
    code, refused = wiki_json(capsys, "maint", "skip", brief["item"], "--reason", "out_of_scope", "--state-dir", st)
    assert code == 1 and refused["detail"]["code"] == "item_not_in_progress"  # not what this run did
    # The run's next item is abandoned mid-edit; its lease expires and the same run begins again.
    code, other = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and other["item"] != brief["item"]
    draft.write_text("half done again\n", encoding="utf-8")
    expire_lease(loop)
    again = begin(capsys, loop, "R-1")
    assert again["swept"]["interrupted"] == 1 and again["reclaimed"] == 1 and not draft.exists()
    code, resumed = wiki_json(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and resumed["item"] in (brief["item"], other["item"])


# --- Codex audit resubmission (design §4.7) --------------------------------------------------------


def audit_parent(loop: dict, parent: str, *classes: str) -> None:
    """A done ingest older than an hour whose Codex audits failed as ``classes``, oldest first."""
    jobs = loop["gate"].root / "kb-a" / ".okf" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{parent}.json").write_text(json.dumps({
        "id": parent, "kind": "ingest", "status": "done", "validation": {"status": "passed"},
        "created": "2026-01-01T00:00:00Z", "finished": "2026-01-01T00:01:00Z", "concept_files": [METRIC],
        "sha256": "0" * 64}), encoding="utf-8")
    for number, cls in enumerate(classes, 1):
        (jobs / f"{parent}-{number}.json").write_text(json.dumps({
            "id": f"{parent}-{number}", "kind": "audit", "parent_job": parent, "status": "failed",
            "created": f"2026-01-0{number + 1}T00:00:00Z", "finished": f"2026-01-0{number + 1}T00:05:00Z",
            "service": {"version": "0.3.0", "build": "build-A"},
            "failure": {"class": cls, "retryable": failure.CLASSES[cls][0], "stage": "agent", "detail": cls}}),
            encoding="utf-8")


def test_begin_resubmits_codex_audits_within_the_p0_caps(loop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(version, "_BUILD", ["build-A"])  # the writer build every failed audit ran on
    audit_parent(loop, "quota0000001", "capacity", "capacity", "capacity")  # quota never counts
    audit_parent(loop, "capped000001", "model_output", "timeout", "capacity", "internal")
    audit_parent(loop, "auth00000001", "auth")  # not retryable: a human first
    audit_parent(loop, "never0000001")  # no audit yet

    begin(capsys, loop, "R-1")

    run = json.loads(next((loop["st"] / "runs").glob("R-1-*/run.json")).read_text(encoding="utf-8"))
    assert sorted(run["audits"]["resubmitted"]) == ["never0000001", "quota0000001"], run["audits"]
    assert sorted(run["audits"]["needs_human"]) == ["auth00000001", "capped000001"]
    assert not run["audits"]["refused"]


def test_a_shadow_bundle_leaves_codex_audits_to_the_admin_cron(loop, capsys) -> None:
    audit_parent(loop, "never0000001")
    config = json.loads(loop["config"].read_text(encoding="utf-8"))
    loop["config"].write_text(json.dumps({**config, "audits": {"resubmit": False}}), encoding="utf-8")

    begun = begin(capsys, loop, "R-1")

    assert begun["audits"] == {"mode": "skipped", "resubmitted": 0, "needs_human": 0}
    assert not [call for call in loop["gate"].calls if call[1].endswith("/audit")]


def test_the_item_verbs_never_guess_between_two_bundles_runs(loop, capsys) -> None:
    # Production and its shadow canary share a runtime host (design §9), so possibly a state dir.
    st = loop["st"]
    begin(capsys, loop, "R-1")
    shadow = st / "runs" / f"current-{maint._slug('kb-b')}.json"
    shadow.write_text(json.dumps({"run": "R-shadow", "bundle": "kb-b"}), encoding="utf-8")

    code, refused = wiki_json(capsys, "maint", "next", "--state-dir", st)
    mine = wiki_json(capsys, "-b", "kb-a", "maint", "next", "--state-dir", st)[0]

    assert code == 2 and "runs of kb-a, kb-b are active here; pass -b <bundle>" in refused["error"]
    assert mine in (0, 10)  # kb-a's own run, whichever item it holds
    assert wiki_json(capsys, "maint", "end", "--run", "R-1", "--state-dir", st)[0] == 0
    assert shadow.is_file() and not (st / "runs" / f"current-{maint._slug('kb-a')}.json").exists()
