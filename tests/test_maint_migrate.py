"""Migration verbs of the maintainer loop (design §3, §9): import-v4, export-v4, import-ledger.

Day 0 of phase 3a seeds the writer's cursors from the latest P0 v4 checkpoint and turns the
P0 ledger's unfinished sources into items; a rollback writes the cursors back as a v4 and
hands unfinished items to the old ``maintain`` as a manifest. The CLI runs in-process
against the real gate.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from gate_fixture import Gate

from aiwiki.cli import main as cli
from aiwiki.cli import maint, maintain
from aiwiki.maint import checkpoint, collect_repos
from aiwiki.runtime import secrets

T0 = "2026-09-20T04:00:00Z"
SHA = "1" * 40


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    gate.calls = gate.connect()
    yield gate
    gate.close()


def wiki_json(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args] + ["--json"])
    except SystemExit as exit_:
        code = exit_.code
    return code, json.loads(capsys.readouterr().out)


def v4(path: Path, *, sha: str = SHA, issues_id: str = "issue-9") -> dict:
    remotes = ("https://code.example/solvely/control.git", "git@code.example:solvely/web.git")
    found = {"version": 4, "repo_root": "/srv/reference", "completed_at": T0,
             "issues": {"updated_at": T0, "id": issues_id},
             "repos": {collect_repos.repo_id(collect_repos.canonical_remote(url)): {
                 "name": url.rsplit("/", 1)[-1].removesuffix(".git"), "remote_url": url, "branch": "main",
                 "sha": sha, **({"stale_since": T0, "last_error": "timed out"} if "web" in url else {})}
                 for url in remotes}}
    path.write_text(json.dumps({"found": True, "checkpoint": found}), encoding="utf-8")  # `checkpoint find` output
    return found


def enqueue(gate: Gate, topic: str, files: dict[str, bytes]) -> str:
    response = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": "repo"}, "topic_key": topic, "brief": topic,
        "files": [{"name": name, "content_b64": base64.b64encode(data).decode(), "origin": {"kind": "git-file"}}
                  for name, data in files.items()]}]})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


def test_import_v4_seeds_the_cursors_and_export_v4_gives_them_back(gate, capsys, tmp_path) -> None:
    original = v4(tmp_path / "find.json")

    code, imported = wiki_json(capsys, "maint", "import-v4", tmp_path / "find.json")

    assert code == 0 and [row["outcome"] for row in imported["cursors"]] == ["created", "created"]
    repos = gate.client.get("/maint/cursors/repos", params={"bundle": "kb-a"}, headers=gate.headers()).json()
    assert repos["value"]["code.example/solvely/web"] == {"branch": "main", "sha": SHA, "stale_since": T0,
                                                          "error": "timed out"}
    # A newer checkpoint never silently overwrites what the writer holds.
    v4(tmp_path / "newer.json", sha="2" * 40)
    code, kept = wiki_json(capsys, "maint", "import-v4", tmp_path / "newer.json")
    assert code == 1 and {row["cursor"]: row["outcome"] for row in kept["cursors"]} == {
        "issues": "unchanged", "repos": "kept"}
    code, same = wiki_json(capsys, "maint", "import-v4", tmp_path / "find.json")
    assert code == 0 and {row["outcome"] for row in same["cursors"]} == {"unchanged"}

    code, exported = wiki_json(capsys, "maint", "export-v4", "--output", tmp_path / "v4.json",
                               "--repo-root", "/srv/reference", "--completed-at", "2026-09-21T04:00:00Z")

    assert code == 0 and exported["checkpoint"] == str(tmp_path / "v4.json")
    back = checkpoint.validate(json.loads((tmp_path / "v4.json").read_text(encoding="utf-8")))
    assert back["issues"] == original["issues"] and back["repo_root"] == "/srv/reference"
    assert {key: (row["sha"], row.get("stale_since")) for key, row in back["repos"].items()} == {
        key: (row["sha"], row.get("stale_since")) for key, row in original["repos"].items()}
    code, error = wiki_json(capsys, "maint", "export-v4", "--completed-at", "2026-09-21T04:00:00Z",
                            "--config", tmp_path / "none.json")
    assert code == 2 and "--repo-root" in error["error"]


def test_export_v4_hands_unfinished_items_back_to_maintain(gate, capsys, tmp_path, monkeypatch) -> None:
    v4(tmp_path / "find.json")
    assert wiki_json(capsys, "maint", "import-v4", tmp_path / "find.json")[0] == 0
    single = enqueue(gate, "repo:x#tasks/a", {"status.md": b"# A\n\ndone\n"})
    run = {**gate.headers(), "X-AIWiki-Run": "WAIO-1"}
    assert gate.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=run).status_code == 200
    assert gate.client.post("/maint/items/next", params={"bundle": "kb-a"}, headers=run).json()["item"]["id"] == single
    newer = enqueue(gate, "repo:x#tasks/a", {"status.md": b"# A\n\nreopened\n"})  # in progress: not merged
    several = enqueue(gate, "repo:x#tasks/b", {"README.md": b"# B\n", "status.md": b"shipped\n"})
    manifest = tmp_path / "rollback" / "sources.json"
    manifest.parent.mkdir()

    code, exported = wiki_json(capsys, "maint", "export-v4", "--output", tmp_path / "v4.json", "--repo-root", "/r",
                               "--pending-manifest", manifest)

    assert code == 0 and exported["manifest"] == {"manifest": str(manifest), "items": 3, "sources": 3}
    sources = json.loads(manifest.read_text(encoding="utf-8"))["sources"]
    by_identity = {source["identity"]: source for source in sources}
    assert sorted(by_identity) == sorted([f"repo:x#tasks/a@{single}", f"repo:x#tasks/a@{newer}",
                                          f"repo:x#tasks/b@{several}"])
    single_path, several_path = (Path(by_identity[identity]["path"]) for identity in (
        f"repo:x#tasks/a@{single}", f"repo:x#tasks/b@{several}"))
    assert single_path.read_bytes() == b"# A\n\ndone\n" and single_path.name == f"{single}.md"  # verbatim
    packet = several_path.read_text(encoding="utf-8")
    assert packet.startswith("---\nai_wiki_evidence: 1\n") and "## S2 · " in packet and several in packet
    # The P0 runner freezes the manifest as it stands (no network: nothing to import) and,
    # one identity per item, runs every source as its own latest version: none is superseded.
    state = {"version": 1, "endpoint": "http://gate.test", "bundle": "kb-a", "sources": []}
    maintain.add_sources(state, json.loads(manifest.read_text(encoding="utf-8")), tmp_path / "ledger", "kb-a")
    assert [entry["sha256"] for entry in state["sources"]] == [source["sha256"] for source in sources]
    latest = {entry["identity"]: entry for entry in state["sources"]}
    assert all(latest[entry["identity"]] is entry for entry in state["sources"])
    # More unfinished items than one listing returns: the export fails rather than drop some.
    monkeypatch.setattr(maint, "LIST_LIMIT", 1)
    code, error = wiki_json(capsys, "maint", "export-v4", "--output", tmp_path / "v4.json", "--repo-root", "/r",
                            "--pending-manifest", manifest)
    assert code == 1 and "2 ready items" in error["error"], error


def ledger_entry(root: Path, identity: str, data: bytes, status: str = "pending", **extra) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    frozen = root / "sources" / sha / "evidence.md"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_bytes(data)
    return {"identity": identity, "sha256": sha, "path": str(frozen), "ingest": [], "audit": [], "status": status,
            **extra}


def writer_job(gate: Gate, job_id: str, status: str) -> None:
    """The writer's live receipt of a P0 ingest, newer than the ledger's last poll of it."""
    jobs = gate.root / "kb-a" / ".okf" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{job_id}.json").write_text(json.dumps({"id": job_id, "kind": "ingest", "status": status}),
                                         encoding="utf-8")


def test_import_ledger_turns_unfinished_sources_into_items(gate, capsys, tmp_path) -> None:
    root = tmp_path / "ledger"
    bad = ledger_entry(root, "tampered", b"original\n")
    Path(bad["path"]).write_bytes(b"edited\n")
    hook = b"Alerts go to https://open.feishu.cn/open-apis/bot/v2/hook/0a1b2c3d-4e5f-6789-abcd-ef0123456789\n"
    writer_job(gate, "j-running", "running")
    writer_job(gate, "j-landed", "done")
    sources = [
        ledger_entry(root, "control/tasks/a", b"# A v1\n"),
        ledger_entry(root, "control/tasks/a", b"# A v2\n"),  # a newer version of the same identity
        ledger_entry(root, "issues/window-3", b"issue window\n", "needs_repair", error="model_output cap"),
        ledger_entry(root, "control/tasks/b", b"# B\n", ingest=[{"id": "j1", "status": "done"}]),
        ledger_entry(root, "control/tasks/c", b"# C\n", "done"),
        ledger_entry(root, "control/tasks/d", b"# D\n", "dropped"),
        bad,
        # P0 hit its poll deadline ("retain job ID and resume"): the writer says whether it landed.
        ledger_entry(root, "control/tasks/e", b"# E\n", ingest=[{"id": "j-running", "status": "running"}]),
        ledger_entry(root, "control/tasks/f", b"# F\n", ingest=[{"id": "j-landed", "status": "running"}]),
        ledger_entry(root, "control/tasks/g", b"# G\n", submitting="ingest"),  # a POST whose answer was lost
        ledger_entry(root, "control/tasks/h", b"# H\n", "needs_repair",
                     ingest=[{"id": "j-unsafe", "status": "failed", "phase": "committed"}]),  # no rollback
        ledger_entry(root, "control/tasks/i", b"# I\n",
                     ingest=[{"id": "j-rolled", "status": "failed", "phase": "rolled_back"}]),
        ledger_entry(root, "notes/alert-bot", hook),
    ]
    (root / "state.json").write_text(json.dumps({"version": 1, "endpoint": "http://gate.test", "bundle": "kb-a",
                                                 "sources": sources}), encoding="utf-8")

    code, imported = wiki_json(capsys, "maint", "import-ledger", "--ledger", root)

    assert code == 0, imported
    assert (imported["imported"], imported["created"], imported["merged"], imported["redactions"]) == (5, 4, 1, 1)
    assert imported["audit_pending"] == ["control/tasks/b", "control/tasks/f"]
    assert imported["in_flight"] == ["control/tasks/e", "control/tasks/g", "control/tasks/h"]
    assert imported["unreadable"] == ["tampered"]
    items = gate.client.get("/maint/items", params={"bundle": "kb-a", "status": "ready"},
                            headers=gate.headers()).json()["items"]
    by_topic = {item["topic_key"]: item for item in items}
    assert sorted(by_topic) == ["ledger:control/tasks/a", "ledger:control/tasks/i", "ledger:issues/window-3",
                                "ledger:notes/alert-bot"]
    newest = by_topic["ledger:control/tasks/a"]
    assert [file["sha256"] for file in newest["files"]] == [sources[1]["sha256"]]
    assert newest["versions"][0]["replaced"][0]["sha256"] == sources[0]["sha256"]
    assert "model_output cap" in by_topic["ledger:issues/window-3"]["brief"]
    # Frozen redacted, as the gate's packet scan requires; the ledger's sha stays in the origin.
    bot = by_topic["ledger:notes/alert-bot"]
    stored = gate.client.get(f"/maint/items/{bot['id']}/files/source.md", params={"bundle": "kb-a"},
                             headers=gate.headers()).content.decode()
    assert stored == "Alerts go to <redacted:webhook_url>\n" and not secrets.scan(stored)
    assert bot["origin"]["sha256"] == hashlib.sha256(hook).hexdigest()
    code, again = wiki_json(capsys, "maint", "import-ledger", "--ledger", root)
    assert code == 0 and (again["created"], again["duplicate"]) == (0, 5)
    (root / "state.json").write_text(json.dumps({"version": 1, "bundle": "kb-b", "sources": []}), encoding="utf-8")
    code, refused = wiki_json(capsys, "maint", "import-ledger", "--ledger", root)
    assert code == 2 and "kb-b" in refused["error"]
