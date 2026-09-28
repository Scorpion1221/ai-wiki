"""Inbox intake (design §6, §10.1): with AIWIKI_INTAKE=inbox a member's upload, pasted text or
link and an out-of-band inbox drop become maintainer work items, never Codex curation. The job
follows its item to the changeset that curated it, the writer never fetches a URL, and
POST /admin/inbox/requeue hands waiting items back to Codex. AIWIKI_INTAKE=curate is today's path.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gate_fixture import EVIDENCE_ID, METRIC, Gate, cited, git

from aiwiki.cli import main as cli
from aiwiki.cli import workspace
from aiwiki.service import inbox, worker
from aiwiki.service import ingest as I
from aiwiki.service import maint_state as M

FAKE_KEY = "AKIA" + "Q" * 16  # secret-shaped, built at runtime
FEISHU = "https://example.feishu.cn/docx/doxcnFAKE"
MEMBER = "member:alice"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, AIWIKI_INTAKE="inbox")
    gate.curated = []  # sources the Codex path was handed; no agent ever runs
    monkeypatch.setattr(worker.curate, "run", lambda _bundle, source, _job_path: gate.curated.append(source))
    monkeypatch.setattr(worker.curate, "AGENT_BIN", sys.executable)  # Codex still exists on this writer

    def send(request):  # the CLI's JSON calls (ingest, jobs, whoami); gate.connect() routes the rest
        url = urllib.parse.urlsplit(request.full_url)
        response = gate.client.request(request.get_method(), url.path, params=urllib.parse.parse_qsl(url.query),
                                       content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            cli._fail(f"server rejected the request (status {response.status_code}): {response.json()['detail']}")
        return response.json()

    monkeypatch.setattr(cli, "_send", send)
    yield gate
    gate.close()


def ingest(gate: Gate, token: str = "member", **body):
    return gate.client.post("/ingest", params={"bundle": "kb-a"}, headers=gate.headers(token), json=body)


def job(gate: Gate, job_id: str, token: str = "member") -> dict:
    response = gate.client.get(f"/jobs/{job_id}", params={"bundle": "kb-a"}, headers=gate.headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def bundle(gate: Gate) -> Path:
    return gate.root / "kb-a"


def claim(gate: Gate, run: str = "WAIO-1") -> dict | None:
    lease = gate.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run=run))
    assert lease.status_code == 200, lease.text
    answer = gate.client.post("/maint/items/next", params={"bundle": "kb-a"}, headers=gate.headers(run=run))
    assert answer.status_code == 200, answer.text
    return answer.json()["item"]


def test_a_member_submission_becomes_a_work_item_not_a_codex_job(gate, monkeypatch) -> None:
    text = f"# Funnel status\n\nThe plugin funnel moved. Deploy key {FAKE_KEY}.\n"
    response = ingest(gate, text=text, title="Funnel status")

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["mode"] == "inbox" and receipt["status"] == "ready" and receipt["deduplicated"] is False
    assert receipt["submitter"] == MEMBER and receipt["via"] == "ingest"
    assert (bundle(gate) / receipt["source"]).read_bytes() == text.encode()  # stored verbatim, as today
    item = M.get_item(bundle(gate), receipt["item"])
    assert item["status"] == "ready" and item["priority"] == 100 and item["collected_by"] == MEMBER
    assert {key: item["origin"][key] for key in ("kind", "submitter", "job", "title", "redactions")} == {
        "kind": "member", "submitter": MEMBER, "job": receipt["id"], "title": "Funnel status", "redactions": 1}
    # The frozen evidence is redacted as collected evidence is, so the gate's secret scan passes it.
    [file] = item["files"]
    frozen = M.read_file(bundle(gate), item["id"], file["name"])
    assert file["name"] == "source.md" and FAKE_KEY not in frozen.decode() and b"<redacted:" in frozen
    stored = I.read_job(bundle(gate), receipt["id"])
    assert stored["status"] == "inbox" and stored["item"] == item["id"] and "curation" not in stored

    # The same content again is the same job; nothing ever reaches the Codex path.
    again = ingest(gate, text=text, title="renamed").json()
    assert again["deduplicated"] is True and again["id"] == receipt["id"] and again["item"] == item["id"]
    assert M.list_items(bundle(gate))["total"] == 1
    read = []  # the verbatim source is known by the sha in its name: the sweep never reads it again
    reader = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read.append(path.name) or reader(path))
    assert worker.sweep_once([bundle(gate)]) == 0
    monkeypatch.setattr(Path, "read_bytes", reader)
    assert Path(receipt["source"]).name not in read
    worker._q.join()
    assert gate.curated == [] and I.active_jobs(bundle(gate)) == []
    assert job(gate, receipt["id"])["status"] == "ready"


def test_a_member_item_goes_through_the_maintainer_loop_to_its_changeset(gate, tmp_path, monkeypatch,
                                                                         capsys) -> None:
    """The curating maintainer's own CLI: begin, next (the member item first), propose, end."""
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("multica", "uv"):  # the curator doctor wants them on PATH
        (tools / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(workspace, "POLL_S", 0.02)
    collected = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": "repo"}, "topic_key": "repo:x#tasks/funnel", "priority": 70, "brief": "task docs",
        "files": [{"name": "S1-status.md", "content_b64": base64.b64encode(b"# Status\n").decode(),
                   "origin": {"kind": "git-file"}}]}]})
    assert collected.status_code == 200, collected.text
    receipt = ingest(gate, text=f"# Funnel status\n\nThe funnel moved; key {FAKE_KEY}.\n", title="Funnel").json()
    config, st = tmp_path / "maint.json", tmp_path / "st"
    config.write_text(json.dumps({"audits": {"resubmit": False}}), encoding="utf-8")
    gate.connect("curator")

    code, begun = run(capsys, "maint", "begin", "--run", "WAIO-9", "--only", "repos", "--config", config,
                      "--state-dir", st)
    assert code == 5 and begun["ready"] == {"member": 1, "repo": 1}, begun  # repos unconfigured: partial
    code, brief = run(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and brief["item"] == receipt["item"] and brief["origin"] == "member", brief
    assert brief["files"] == [{"name": "source.md", "bytes": len(M.read_file(bundle(gate), brief["item"],
                                                                             "source.md"))}]
    ws = Path(brief["commands"][0].split()[3])
    (ws / METRIC).write_text(cited((ws / METRIC).read_text(encoding="utf-8")), encoding="utf-8")
    code, verdict = run(capsys, "propose", "--dir", ws, "--item", brief["item"], "--source-id", EVIDENCE_ID,
                        "--state-dir", st)
    assert code == 0 and verdict["status"] == "done", verdict
    code, report = run(capsys, "maint", "end", "--run", "WAIO-9", "--state-dir", st)
    assert report["queue"]["curated"] == 1

    gate.connect("member")  # the member follows the job id it was given
    code, followed = run(capsys, "jobs", receipt["id"])
    assert code == 0 and followed["status"] == "curated" and followed["item"] == receipt["item"]
    assert followed["changeset"] == verdict["id"] and followed["commit"] == gate.remote_head()
    packet = next((bundle(gate) / "sources").glob(f"{EVIDENCE_ID}-*"))
    assert FAKE_KEY not in packet.read_text(encoding="utf-8")


def run(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args] + ["--json"])
    except SystemExit as exit_:
        code = exit_.code
    out = capsys.readouterr().out
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


def test_the_writer_never_fetches_a_link(gate, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("the writer opened a connection")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    receipt = ingest(gate, url=FEISHU, title="Retry cap").json()

    assert receipt["status"] == "needs_access" and "never fetches URLs" in receipt["reason"]
    assert "source" not in receipt and not list((bundle(gate) / "sources" / "inbox").glob("*"))
    item = M.get_item(bundle(gate), receipt["item"])
    assert item["origin"]["url"] == FEISHU
    assert M.read_file(bundle(gate), item["id"], "link.txt") == f"{FEISHU}\n".encode()
    assert claim(gate) is None  # closed at once: no maintainer spends a run on it
    assert ingest(gate, url=FEISHU).json()["deduplicated"] is True
    for url in ("ftp://example.com/x", "https://user:" + "s3cr3tvalue@example.com/doc", "https://a b"):
        assert ingest(gate, url=url).status_code == 400, url
    assert ingest(gate, url=FEISHU, fetched={"cookie": "x"}).status_code == 400
    # A format nothing reads is kept, and says why it waits.
    pdf = ingest(gate, content_b64=base64.b64encode(b"%PDF-1.4 data").decode(), filename="q3.pdf").json()
    assert pdf["status"] == "needs_conversion" and "convert it" in pdf["reason"]
    assert M.get_item(bundle(gate), pdf["item"])["files"][0]["name"] == "source.pdf"


def test_the_sweep_registers_inbox_drops_as_member_items(gate) -> None:
    drop = bundle(gate) / "sources" / "inbox" / "handover.md.source"
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_bytes(b"# Handover\n\nThe funnel owner changed.\n")

    assert worker.sweep_once([bundle(gate)]) == 1
    assert worker.sweep_once([bundle(gate)]) == 0
    [item] = M.list_items(bundle(gate))["items"]
    assert item["origin"]["via"] == "drop" and item["origin"]["submitter"] is None and item["status"] == "ready"
    assert item["files"][0]["name"] == "source.md" and item["brief"] == "inbox drop: handover.md"
    stored = I.read_job(bundle(gate), item["origin"]["job"])
    assert stored["item"] == item["id"] and stored["source"] == "sources/inbox/handover.md.source"
    worker._q.join()
    assert gate.curated == []


def test_requeue_hands_waiting_member_items_back_to_codex(gate, tmp_path, monkeypatch, capsys) -> None:
    ids = [ingest(gate, text=f"# Note {n}\n\nfact {n}\n").json()["id"] for n in range(4)]
    items = {I.read_job(bundle(gate), job_id)["item"]: job_id for job_id in ids}
    parked = claim(gate)["id"]
    parking = gate.client.post(f"/maint/items/{parked}/resolve", params={"bundle": "kb-a"},
                               headers=gate.headers(run="WAIO-1"),
                               json={"outcome": "parked", "class": "model_output", "reason": "yaml twice"})
    assert parking.status_code == 200, parking.text
    held = gate.client.post("/maint/items/next", params={"bundle": "kb-a"},
                            headers=gate.headers(run="WAIO-1")).json()["item"]["id"]
    ready = sorted(set(items) - {parked, held})
    gone = ready.pop()  # its verbatim source was lost: it stays in the queue
    (bundle(gate) / I.read_job(bundle(gate), items[gone])["source"]).unlink()
    route = {"params": {"bundle": "kb-a"}, "json": {"reason": "intake rolled back"}}
    assert gate.client.post("/admin/inbox/requeue", headers=gate.headers("curator"), **route).status_code == 403
    monkeypatch.setattr(worker.curate, "AGENT_BIN", str(tmp_path / "no-codex"))
    gone_codex = gate.client.post("/admin/inbox/requeue", headers=gate.headers("owner"), **route)
    assert gone_codex.status_code == 409 and "Codex path is gone" in gone_codex.json()["detail"]
    monkeypatch.setattr(worker.curate, "AGENT_BIN", sys.executable)

    gate.connect("owner")
    capsys.readouterr()
    code = cli.main(["admin", "inbox", "requeue", "--reason", "intake rolled back", "--json"])
    result = json.loads(capsys.readouterr().out)

    assert code == 1 and result["held"] == [held] and [row["item"] for row in result["unavailable"]] == [gone]
    assert sorted(row["item"] for row in result["requeued"]) == sorted([parked, *ready])
    worker._q.join()
    for item_id in (parked, *ready):
        item = M.get_item(bundle(gate), item_id)
        assert item["status"] == "requeued" and item["resolution"]["reason"] == "intake rolled back"
        followed = job(gate, items[item_id])  # an ordinary Codex ingest from now on
        assert followed["status"] == "queued" and followed["requeued"]["item"] == item_id
        assert followed["source"] in gate.curated and "mode" not in followed
    assert M.get_item(bundle(gate), held)["status"] == "in_progress"
    assert M.get_item(bundle(gate), gone)["status"] == "ready"
    # Nothing is handed over twice; a requeue that stopped before its job was queued finishes.
    late = ingest(gate, text="# Late\n\nlate fact\n").json()
    M.requeue(bundle(gate), [late["item"]], principal="human:owner", reason=None)
    # .okf is writable by the in-place Codex audit: an item naming a job outside .okf/jobs stays.
    forged = ingest(gate, text="# Forged\n\nforged fact\n").json()["item"]
    record = bundle(gate) / ".okf" / "maint" / "items" / forged / "item.json"
    value = json.loads(record.read_text(encoding="utf-8"))
    value["origin"]["job"] = "../../../x"
    record.write_text(json.dumps(value), encoding="utf-8")
    again = gate.client.post("/admin/inbox/requeue", headers=gate.headers("owner"), **route).json()
    assert [row["item"] for row in again["requeued"]] == [late["item"]]
    assert {row["item"] for row in again["unavailable"]} == {gone, forged}
    worker._q.join()
    assert len(gate.curated) == 3 and job(gate, late["id"])["status"] == "queued"


def test_curate_intake_is_todays_codex_path(tmp_path, monkeypatch) -> None:
    gate = Gate(tmp_path, monkeypatch)
    curated = []
    monkeypatch.setattr(worker.curate, "run", lambda _bundle, source, _job_path: curated.append(source))
    monkeypatch.setattr(worker.curate, "AGENT_BIN", sys.executable)
    try:
        receipt = ingest(gate, text="# Note\n\nfact\n", title="note").json()
        assert receipt["status"] == "queued" and receipt["curation"] == "queued" and "item" not in receipt
        assert ingest(gate, url=FEISHU).status_code == 400  # a link alone is no source, as before
        drop = bundle(gate) / "sources" / "inbox" / "drop.md.source"
        drop.write_bytes(b"# Drop\n")
        assert worker.sweep_once([bundle(gate)]) == 1
        worker._q.join()
        assert curated == [receipt["source"], "sources/inbox/drop.md.source"]
        assert M.list_items(bundle(gate))["total"] == 0
        assert gate.client.post("/admin/inbox/requeue", params={"bundle": "kb-a"},
                                headers=gate.headers("owner")).json() == {"requeued": [], "held": [],
                                                                         "unavailable": []}
    finally:
        gate.close()


def test_member_items_come_first_and_age_like_every_item(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kb"
    (root / ".okf" / "jobs").mkdir(parents=True)
    now = [datetime(2026, 10, 1, tzinfo=UTC)]
    monkeypatch.setattr(M, "_now", lambda: now[0])
    principal, files = "process:ai-wiki-maintainer", [{"name": "a.md", "content_b64": base64.b64encode(b"a").decode()}]

    def collected(topic: str) -> str:
        return M.enqueue(root, [{"origin": {"kind": "repo"}, "topic_key": topic, "priority": 40, "files": files}],
                         principal=principal)["items"][0]["id"]

    def member(text: str) -> str:
        return inbox.receive(root, text.encode(), filename=None, title=None, url=None, fetched=None,
                             submitter=MEMBER)[0]["item"]

    def claimed() -> str:
        M.acquire_lease(root, "maintainer", principal=principal, run="WAIO-1")
        item = M.next_item(root, principal=principal, run="WAIO-1")["item"]["id"]
        M.release_lease(root, "maintainer", principal=principal, run="WAIO-1")
        M.admin_resolve(root, item, {"outcome": "skipped", "reason": "out_of_scope"}, principal="human:owner")
        return item

    old = collected("repo:x#old")  # 40, plus 5 a day it waits
    now[0] += timedelta(days=10)
    first, second = member("first"), member("second")
    assert [claimed(), claimed(), claimed()] == [first, second, old]  # 100 before 90; members FIFO
    ancient = collected("repo:x#ancient")
    now[0] += timedelta(days=13)
    fresh = member("fresh")
    assert [claimed(), claimed()] == [ancient, fresh]  # 105: ageing starves nothing, members included


def test_cli_reads_feishu_links_as_the_member_and_submits_the_content(gate, tmp_path, monkeypatch,
                                                                     capsys) -> None:
    tools, log = tmp_path / "lark-bin", tmp_path / "lark-args"
    tools.mkdir()
    document = {"ok": True, "data": {"document": {"document_id": "doxcnFAKE", "revision_id": 7,
                                                   "content": "# Retry cap\n\nWe cap retries at 3.\n"}}}
    fake = tools / "lark-cli"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{log}'\nprintf '%s' '{json.dumps(document)}'\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    gate.connect("member")

    code, out = run(capsys, "ingest", FEISHU)

    [row] = out["submissions"]
    assert code == 0 and row["state"] == "ready" and "detail" not in row, out
    assert log.read_text(encoding="utf-8").split() == ["docs", "+fetch", "--doc", FEISHU, "--doc-format", "markdown"]
    item = M.get_item(bundle(gate), job(gate, row["job"])["item"])
    assert item["origin"]["url"] == FEISHU and item["origin"]["title"] == "Retry cap"
    assert item["origin"]["fetched"] == {"tool": "lark-cli", "document_id": "doxcnFAKE", "revision_id": 7}
    assert M.read_file(bundle(gate), item["id"], "source.md") == b"# Retry cap\n\nWe cap retries at 3.\n"

    # lark-cli that cannot read it, or none at all: the link alone, needs_access, and why.
    fake.write_text("#!/bin/sh\nprintf '%s' '{\"ok\": false}'\nexit 1\n", encoding="utf-8")
    code, out = run(capsys, "ingest", FEISHU + "?from=wiki")
    assert code == 0 and out["submissions"][0]["state"] == "needs_access"
    assert out["submissions"][0]["detail"].startswith("lark-cli could not read it; the writer never fetches URLs")
    fake.unlink()
    code, out = run(capsys, "ingest", "https://example.com/post")  # not Feishu: never handed to lark-cli
    assert code == 0 and out["submissions"][0]["state"] == "needs_access"
    assert out["submissions"][0]["detail"].startswith("the writer never fetches URLs")
    code, out = run(capsys, "ingest", FEISHU + "?v=2")
    assert out["submissions"][0]["detail"].startswith("lark-cli is not installed here;")

    gate.app()  # AIWIKI_INTAKE=curate: the CLI says it needs the content, before sending anything
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:
        cli.main(["ingest", "https://example.com/other"])
    assert refused.value.code == 2 and "takes content, not links" in capsys.readouterr().out


def test_member_uploads_keep_binary_evidence_verbatim(gate) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    receipt = ingest(gate, content_b64=base64.b64encode(png).decode(), filename="Funnel Chart.PNG").json()
    item = M.get_item(bundle(gate), receipt["item"])
    assert receipt["status"] == "ready" and item["origin"]["filename"] == "Funnel Chart.PNG"
    assert M.read_file(bundle(gate), item["id"], "source.PNG") == png and item["origin"]["redactions"] == 0
    assert git(bundle(gate), "status", "--porcelain") == ""  # sources/inbox and .okf stay out of Git


def test_inbox_intake_applies_to_bundles_that_commit(gate) -> None:
    """kb-b only dry-runs changesets: no maintainer curates it, so its submissions keep Codex."""
    kb_b = gate.root / "kb-b"
    receipt = gate.client.post("/ingest", params={"bundle": "kb-b"}, headers=gate.headers("member"),
                               json={"text": "# kb-b note\n\nfact\n"}).json()
    assert receipt["status"] == "queued" and receipt["curation"] == "queued" and "item" not in receipt
    drop = kb_b / "sources" / "inbox" / "drop.md.source"
    drop.write_bytes(b"# Drop\n")
    assert worker.sweep_once([kb_b]) == 1
    worker._q.join()
    assert gate.curated == [receipt["source"], "sources/inbox/drop.md.source"]
    assert M.list_items(kb_b)["total"] == 0
