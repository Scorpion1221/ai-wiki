"""Inbox intake (design §6, §10.1): with AIWIKI_INTAKE=inbox a member's upload, pasted text or
link and an out-of-band inbox drop become maintainer work items of a committing bundle, never
Codex curation, and their redacted copies are committed and pushed at once. The job follows its
item to the changeset that curated it, the writer never fetches a URL (the maintainer reads a
Feishu link as the wiki's app), a quota bounds what one principal queues, and POST
/admin/inbox/requeue hands unfinished items back to Codex. AIWIKI_INTAKE=curate is today's path.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gate_fixture import EVIDENCE_ID, METRIC, Gate, cited, git

from aiwiki.cli import main as cli
from aiwiki.cli import workspace
from aiwiki.runtime import audit, changeset, curate
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


def requeue(gate: Gate, token: str = "owner", **body):
    return gate.client.post("/admin/inbox/requeue", params={"bundle": "kb-a"}, headers=gate.headers(token), json=body)


def maintainer_host(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """PATH as the curator doctor wants it, with a lark-cli that reads only doxcnOK; ``(config,
    state dir, lark-cli argument log)``."""
    tools, log = tmp_path / "bin", tmp_path / "lark-args"
    tools.mkdir()
    content = f"# Retry cap\n\nWe cap retries at 3; key {FAKE_KEY}.\n"
    document = {"ok": True, "data": {"document": {"document_id": "doxcnOK", "revision_id": 3, "content": content}}}
    scripts = {"multica": "exit 0\n", "uv": "exit 0\n",
               "lark-cli": f"printf '%s\\n' \"$@\" >> '{log}'\ncase \"$*\" in\n  *doxcnOK*) printf '%s' "
                           f"'{json.dumps(document)}' ;;\n  *) printf '%s' '{{\"ok\": false}}'; exit 1 ;;\nesac\n"}
    for name, script in scripts.items():
        (tools / name).write_text("#!/bin/sh\n" + script, encoding="utf-8")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(workspace, "POLL_S", 0.02)
    config = tmp_path / "maint.json"
    config.write_text(json.dumps({"audits": {"resubmit": False}}), encoding="utf-8")
    return config, tmp_path / "st", log


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
    config, st, _log = maintainer_host(tmp_path, monkeypatch)
    collected = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": "repo"}, "topic_key": "repo:x#tasks/funnel", "priority": 70, "brief": "task docs",
        "files": [{"name": "S1-status.md", "content_b64": base64.b64encode(b"# Status\n").decode(),
                   "origin": {"kind": "git-file"}}]}]})
    assert collected.status_code == 200, collected.text
    receipt = ingest(gate, text=f"# Funnel status\n\nThe funnel moved; key {FAKE_KEY}.\n", title="Funnel").json()
    intake = receipt["intake"]
    assert intake["status"] == "committed" and intake["commit"] == gate.remote_head(), receipt
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
    # The changeset cites the committed item: its packet is the same Git blob as the member's
    # copy, which stays where the intake committed it.
    assert followed["intake"] == intake
    git(gate.remote, "merge-base", "--is-ancestor", intake["commit"], "main")
    assert git(gate.remote, "rev-parse", f"main:{intake['path']}") == git(
        gate.remote, "rev-parse", f"main:sources/{packet.name}")


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

    # A Feishu link waits for the maintainer, who reads it as the wiki's app (maint next).
    assert receipt["status"] == "ready" and "reason" not in receipt
    assert "source" not in receipt and not list((bundle(gate) / "sources" / "inbox").glob("*"))
    item = M.get_item(bundle(gate), receipt["item"])
    assert item["origin"]["url"] == FEISHU
    assert M.read_file(bundle(gate), item["id"], "link.txt") == f"{FEISHU}\n".encode()
    assert ingest(gate, url=FEISHU).json()["deduplicated"] is True
    # Any other link nothing here reads: needs_access at once, no maintainer spends a run on it.
    other = ingest(gate, url="https://example.com/post").json()
    assert other["status"] == "needs_access" and "never fetches URLs" in other["reason"]
    assert claim(gate)["id"] == receipt["item"]
    for url in ("ftp://example.com/x", "https://user:" + "s3cr3tvalue@example.com/doc", "https://a b"):
        assert ingest(gate, url=url).status_code == 400, url
    assert ingest(gate, url=FEISHU, fetched={"cookie": "x"}).status_code == 400
    assert ingest(gate, text="# Note\n", title=f"notes {FAKE_KEY}").status_code == 400  # it travels with the item
    # A format nothing reads is kept, and says why it waits.
    pdf = ingest(gate, content_b64=base64.b64encode(b"%PDF-1.4 data").decode(), filename="q3.pdf").json()
    assert pdf["status"] == "needs_conversion" and "convert it" in pdf["reason"]
    assert M.get_item(bundle(gate), pdf["item"])["files"][0]["name"] == "source.pdf"


def test_the_sweep_registers_inbox_drops_as_member_items_and_commits_them(gate) -> None:
    drop = bundle(gate) / "sources" / "inbox" / "handover.md.source"
    drop.parent.mkdir(parents=True, exist_ok=True)
    drop.write_bytes(f"# Handover\n\nThe funnel owner changed; key {FAKE_KEY}.\n".encode())

    assert worker.sweep_once([bundle(gate)]) == 1
    worker._q.join()
    assert worker.sweep_once([bundle(gate)]) == 0  # nor is its committed copy a drop
    [item] = M.list_items(bundle(gate))["items"]
    assert item["origin"]["via"] == "drop" and item["origin"]["submitter"] is None and item["status"] == "ready"
    assert item["files"][0]["name"] == "source.md" and item["brief"] == "inbox drop: handover.md"
    stored = I.read_job(bundle(gate), item["origin"]["job"])
    assert stored["item"] == item["id"] and stored["source"] == "sources/inbox/handover.md.source"
    worker._q.join()
    assert gate.curated == []
    # The drop is committed like a submission, redacted; the drop itself stays out of Git.
    committed = item["intake"]
    assert committed["commit"] == gate.remote_head() and committed["path"].startswith("sources/inbox/intake/handover-")
    assert git(gate.remote, "log", "-1", "--format=%s", "main") == "intake: handover.md (service)"
    assert git(gate.remote, "show", f"main:{committed['path']}").startswith("# Handover")
    assert FAKE_KEY not in git(gate.remote, "log", "-p", "main")
    assert git(bundle(gate), "status", "--porcelain") == "" and drop.is_file()


def test_a_submission_is_committed_redacted_at_once_and_a_resend_commits_nothing(gate, tmp_path,
                                                                                capsys) -> None:
    head = gate.remote_head()
    notes = tmp_path / "notes.md"
    notes.write_text(f"# Funnel status\n\nThe plugin funnel moved. Deploy key {FAKE_KEY}.\n", encoding="utf-8")
    gate.connect("member")

    code, out = run(capsys, "ingest", notes)

    [row] = out["submissions"]
    assert code == 0 and row["state"] == "ready" and row["commit"] == gate.remote_head() != head, row
    followed = run(capsys, "jobs", row["job"])[1]  # ai-wiki jobs shows the commit
    intake = followed["intake"]
    assert intake["status"] == "committed" and intake["commit"] == row["commit"], followed
    assert intake["path"].startswith("sources/inbox/intake/notes-") and intake["path"].endswith(".md.source")
    item = M.get_item(bundle(gate), followed["item"])
    assert {key: item["intake"][key] for key in ("job", "path", "commit")} == {
        key: intake[key] for key in ("job", "path", "commit")}
    message = git(gate.remote, "log", "-1", "--format=%B", "main").splitlines()
    assert message[0] == "intake: notes.md (member:alice)"
    assert f"Intake: {intake['job']}" in message and f"Work-Items: {item['id']}" in message
    assert git(gate.remote, "show", "--name-only", "--format=", "main") == intake["path"]
    # What Git holds is the item's frozen, redacted file; the verbatim upload stays on the writer.
    assert git(gate.remote, "show", f"main:{intake['path']}") + "\n" == M.read_file(
        bundle(gate), item["id"], "source.md").decode()
    assert FAKE_KEY not in git(gate.remote, "log", "-p", "main") and FAKE_KEY in gate.read(followed["source"])
    assert git(bundle(gate), "status", "--porcelain") == ""
    # A service commit: the audit backlog never takes it for a push past the writer.
    assert audit.external_changes(bundle(gate), datetime(2026, 1, 1, tzinfo=UTC))[intake["path"]][2] is False

    # The same content again is the same job: no second commit, now or from the sweep.
    again = ingest(gate, content_b64=base64.b64encode(notes.read_bytes()).decode(), filename="notes.md").json()
    assert again["deduplicated"] is True and again["intake"] == intake
    assert worker.sweep_once([bundle(gate)]) == 0
    worker._q.join()
    assert gate.remote_head() == intake["commit"] and M.list_items(bundle(gate))["total"] == 1


def test_a_failed_push_leaves_the_item_ready_and_is_retried(gate, monkeypatch) -> None:
    head = gate.remote_head()
    real = curate._git

    def rejected(root, *args, **kwargs):
        if args[:1] == ("push",):
            return subprocess.CompletedProcess(args, 1, "", "remote: rejected")
        return real(root, *args, **kwargs)

    monkeypatch.setattr(curate, "_git", rejected)
    receipt = ingest(gate, text="# Retry cap\n\nWe cap retries at 3.\n", title="Retry cap").json()

    intake = receipt["intake"]
    assert receipt["status"] == "ready" and intake["status"] == "failed", receipt
    assert intake["detail"].startswith("not in the wiki's Git yet: intake git commit/push failed: push rejected")
    assert intake["detail"].endswith("after 4 attempts; the writer retries it") and "commit" not in intake
    stored = I.read_job(bundle(gate), intake["job"])
    assert (stored["phase"], stored["failure"]["class"]) == ("rolled_back", "transient")
    assert gate.remote_head() == gate.head() == head and git(bundle(gate), "status", "--porcelain") == ""
    assert "intake" not in M.get_item(bundle(gate), receipt["item"])

    monkeypatch.setattr(curate, "_git", real)
    worker.sweep_once([bundle(gate)])  # not due yet
    worker._q.join()
    assert job(gate, receipt["id"])["intake"]["status"] == "failed"
    now = M._now
    monkeypatch.setattr(M, "_now", lambda: now() + timedelta(seconds=stored["failure"]["retry_after_s"]))
    worker.sweep_once([bundle(gate)])
    worker._q.join()

    followed = job(gate, receipt["id"])
    assert followed["status"] == "ready" and followed["intake"]["status"] == "committed", followed
    assert followed["intake"]["commit"] == gate.remote_head() != head
    assert I.read_job(bundle(gate), intake["job"])["attempt"] == 2
    assert M.get_item(bundle(gate), receipt["item"])["intake"]["commit"] == gate.remote_head()


def test_an_intake_commit_killed_after_its_push_is_recorded_by_recover(gate, monkeypatch) -> None:
    """A lost acknowledgement or a writer killed after the push: the remote holds the commit."""
    monkeypatch.setattr(worker, "submit_intake", lambda *_args: None)  # run it by hand below
    receipt = ingest(gate, text="# Retry cap\n\nWe cap retries at 3.\n", title="Retry cap").json()
    assert receipt["intake"]["status"] == "queued"
    path = I.job_path(bundle(gate), receipt["intake"]["job"])
    real = curate._git

    def killed_after_push(root, *args, **kwargs):
        result = real(root, *args, **kwargs)
        if args[:1] == ("push",):
            raise _Killed
        return result

    monkeypatch.setattr(curate, "_git", killed_after_push)
    with pytest.raises(_Killed):
        worker._run_intake(bundle(gate), path)
    monkeypatch.setattr(curate, "_git", real)
    assert I.read_job(bundle(gate), path.stem)["status"] == "running"

    assert worker.recover([bundle(gate)]) is True

    stored = I.read_job(bundle(gate), path.stem)
    assert (stored["status"], stored["recovered"]) == ("done", "remote_contains_commit")
    assert stored["commit"] == gate.remote_head() == gate.head()
    assert M.get_item(bundle(gate), receipt["item"])["intake"]["commit"] == stored["commit"]
    assert job(gate, receipt["id"])["intake"]["status"] == "committed"


def test_an_intake_job_commits_one_plain_file_in_the_intake_folder(gate, monkeypatch) -> None:
    """.okf is not trusted: an intake job whose path leaves the intake folder commits nothing."""
    monkeypatch.setattr(worker, "submit_intake", lambda *_args: None)
    receipt = ingest(gate, text="# Note\n\nfact\n").json()
    path = I.job_path(bundle(gate), receipt["intake"]["job"])
    head = gate.remote_head()
    for forged in ("sources/inbox/intake/../../../metrics/x.md.source", "metrics/x.md.source",
                   "sources/inbox/intake/x.md"):
        I._write_atomic(path, {**I.read_job(bundle(gate), path.stem), "status": "queued", "path": forged})
        worker._run_intake(bundle(gate), path)
        stored = I.read_job(bundle(gate), path.stem)
        assert stored["status"] == "failed" and "not a plain file path" in stored["error"], forged
    assert gate.remote_head() == gate.head() == head and git(bundle(gate), "status", "--porcelain") == ""
    assert not (bundle(gate) / "metrics" / "x.md.source").exists()


class _Killed(BaseException):
    """The writer process dies here; nothing below this frame runs."""


def test_secrets_never_reach_git(gate, monkeypatch) -> None:
    """A title with a secret is refused as before; content and a file name are redacted before
    they are committed, and a copy that still matched a secret rule would not be committed."""
    head = gate.remote_head()
    assert ingest(gate, text="# Note\n", title=f"notes {FAKE_KEY}").status_code == 400
    assert M.list_items(bundle(gate))["total"] == 0 and gate.remote_head() == head
    named = ingest(gate, text="# Named\n\nfact\n", filename=f"{FAKE_KEY}.md").json()["intake"]
    assert named["status"] == "committed" and FAKE_KEY not in named["path"]
    assert FAKE_KEY not in git(gate.remote, "log", "--format=%B", "--name-only", "main")
    head = gate.remote_head()

    monkeypatch.setattr(inbox.secrets, "redact", lambda text: (text, 0))  # a rule's redaction that misses
    receipt = ingest(gate, text=f"# Deploy\n\nkey {FAKE_KEY}\n").json()

    intake = receipt["intake"]
    assert receipt["status"] == "ready" and intake["status"] == "failed", receipt
    assert "still matches secret rule aws_access_key_id" in intake["detail"] and intake["detail"].endswith(
        "does not retry it, tell the owner")
    worker.sweep_once([bundle(gate)])
    worker._q.join()
    assert job(gate, receipt["id"])["intake"]["status"] == "failed" and gate.remote_head() == head
    assert [path.name for path in (bundle(gate) / inbox.INTAKE_DIR).iterdir()] == [Path(named["path"]).name]


def test_only_text_and_images_are_committed_at_intake(gate) -> None:
    """Nothing redacts or scans bytes that are not UTF-8: a file the writer cannot read closes
    needs_conversion and is never committed, whatever it holds, nor is such a drop."""
    head = gate.remote_head()
    unreadable = {"notes.txt": f"café, key {FAKE_KEY}\n".encode("latin-1"),
                  "env.txt": f"KEY={FAKE_KEY}\n".encode("utf-16"),
                  "vault.kdbx": b"\x03\xd9\xa2\x9a" + FAKE_KEY.encode(),
                  "q3.pdf": b"%PDF-1.4\n" + FAKE_KEY.encode()}
    receipts = {}
    for filename, data in unreadable.items():
        receipts[filename] = ingest(gate, content_b64=base64.b64encode(data).decode(), filename=filename).json()
        assert receipts[filename]["status"] == "needs_conversion" and "intake" not in receipts[filename]
    drop = bundle(gate) / "sources" / "inbox" / "dump.csv"
    drop.write_bytes(f"name;key\nJosé;{FAKE_KEY}\n".encode("latin-1"))
    assert worker.sweep_once([bundle(gate)]) == 1
    worker._q.join()
    assert gate.remote_head() == head and not (bundle(gate) / inbox.INTAKE_DIR).exists()

    # An intake job queued for such bytes all the same commits nothing either.
    item = M.get_item(bundle(gate), receipts["notes.txt"]["item"])
    frozen = M.read_file(bundle(gate), item["id"], "source.txt")
    intake = inbox._new_intake(bundle(gate), item["id"], receipts["notes.txt"]["id"], "source.txt", frozen,
                               submitter=MEMBER, filename="notes.txt", title=None, label="notes.txt")
    worker._run_intake(bundle(gate), I.job_path(bundle(gate), intake))
    stored = I.read_job(bundle(gate), intake)
    assert stored["status"] == "failed" and stored["error"] == "only text and images are committed at intake"
    assert stored["failure"]["retryable"] is False and gate.remote_head() == gate.head() == head


def test_intake_commits_only_while_inbox_intake_applies_and_the_submitter_may_submit(gate, monkeypatch) -> None:
    """A rollback to curate or AIWIKI_DISABLE=changesets stops intake commits as it stops
    changesets: a queued one waits, uncommitted, until intake applies again. The submission of a
    principal revoked meanwhile is never committed (design §8.5)."""
    submit = worker.submit_intake
    monkeypatch.setattr(worker, "submit_intake", lambda *_args: None)  # queued, and left there
    first = ingest(gate, text="# Retry cap\n\nWe cap retries at 3.\n", title="Retry cap").json()
    path = I.job_path(bundle(gate), first["intake"]["job"])
    head, handed = gate.remote_head(), []

    for switch in ({"COMMIT_BUNDLES": frozenset()}, {"INTAKE": "curate"}):
        with monkeypatch.context() as patch:
            for name, value in switch.items():
                patch.setattr(worker, name, value)
            patch.setattr(worker, "submit_intake", lambda _bundle, pending: handed.append(pending))
            worker.sweep_once([bundle(gate)])
            worker._run_intake(bundle(gate), path)  # one already in the queue at the switch
            assert handed == [] and I.read_job(bundle(gate), path.stem)["status"] == "queued", switch
            assert gate.remote_head() == head
    monkeypatch.setattr(worker, "submit_intake", submit)
    worker.sweep_once([bundle(gate)])
    worker._q.join()
    assert job(gate, first["id"])["intake"]["status"] == "committed" and gate.remote_head() != head

    monkeypatch.setattr(worker, "submit_intake", lambda *_args: None)
    second = ingest(gate, text="# Funnel\n\nThe funnel moved.\n", title="Funnel").json()
    principals = gate.tmp / "principals.json"
    kept = [entry for entry in json.loads(principals.read_text(encoding="utf-8"))["principals"]
            if entry["id"] != MEMBER]
    principals.write_text(json.dumps({"principals": kept}), encoding="utf-8")
    assert gate.appmod.AUTH.reload() is True
    head = gate.remote_head()
    monkeypatch.setattr(worker, "submit_intake", submit)
    worker.sweep_once([bundle(gate)])
    worker._q.join()

    intake = job(gate, second["id"], token="owner")["intake"]
    assert intake["status"] == "failed" and intake["detail"] == (
        f"not in the wiki's Git yet: {MEMBER} may no longer submit to this bundle; nothing is committed; "
        "the writer does not retry it, tell the owner")
    assert gate.remote_head() == head


def test_raw_uploads_and_job_state_are_never_served_nor_in_a_workspace(gate) -> None:
    """A reader sees the committed, redacted intake copy; the verbatim upload beside it and the
    writer's .okf state are private, and no workspace carries the intake copies."""
    receipt = ingest(gate, text=f"# Deploy\n\nkey {FAKE_KEY}\n", title="Deploy").json()
    committed = receipt["intake"]
    assert committed["status"] == "committed"
    reader = gate.headers("auditor")

    def get(route: str, **params):
        return gate.client.get(route, params={"bundle": "kb-a", **params}, headers=reader)

    raw = Path(receipt["source"]).name
    for private in (receipt["source"], f"sources/intake/../inbox/{raw}", f".okf/jobs/{receipt['id']}.json"):
        response = get("/cat", path=private)
        assert response.status_code == 400 and FAKE_KEY not in response.text, private
    assert get("/ls", dir=".okf/jobs").status_code == 400
    assert [entry["path"] for entry in get("/ls", dir="sources/inbox").json()["items"]] == [
        "sources/inbox/intake/"]
    listed = [entry["path"] for entry in get("/ls", recursive=True, show_all=True).json()["items"]]
    assert committed["path"] in listed and not [path for path in listed if path.startswith(".okf")]
    assert receipt["source"] not in listed
    served = get("/cat", path=committed["path"]).json()["content"]
    assert served.startswith("# Deploy") and FAKE_KEY not in served and "<redacted:" in served

    workspace = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("curator"))
    assert workspace.status_code == 200 and workspace.headers["X-AIWiki-Revision"] == committed["commit"]
    with tarfile.open(fileobj=io.BytesIO(workspace.content)) as tree:
        names = tree.getnames()
    assert "index.md" in names and not [name for name in names if name.startswith("sources/inbox")]


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
    gate.app()  # rolled back to curate: a resend of a waiting item's content answers from that item
    resent = ingest(gate, text="# Forged\n\nforged fact\n").json()
    assert resent["deduplicated"] is True and resent["status"] == "ready" and resent["item"] == forged


def test_curate_intake_is_todays_codex_path(tmp_path, monkeypatch) -> None:
    gate = Gate(tmp_path, monkeypatch)
    curated = []
    monkeypatch.setattr(worker.curate, "run", lambda _bundle, source, _job_path: curated.append(source))
    monkeypatch.setattr(worker.curate, "AGENT_BIN", sys.executable)
    head = gate.remote_head()
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
        assert gate.remote_head() == head and "intake" not in receipt  # nothing is committed at intake
        assert gate.client.post("/admin/inbox/requeue", params={"bundle": "kb-a"},
                                headers=gate.headers("owner")).json() == {"requeued": [], "held": [],
                                                                         "unavailable": []}
    finally:
        gate.close()


def test_member_items_come_first_and_ageing_never_passes_them(tmp_path, monkeypatch) -> None:
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
    # Members come first whatever the backlog's age (ancient is at 105): a member's fresh
    # submission never waits a day behind it, and the ancient item still comes right after.
    assert [claimed(), claimed()] == [fresh, ancient]


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
    (tools / "git").symlink_to(shutil.which("git"))  # the writer, in this process, commits each intake
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

    # lark-cli that cannot read it, or none at all: the link alone waits for the maintainer.
    fake.write_text("#!/bin/sh\nprintf '%s' '{\"ok\": false}'\nexit 1\n", encoding="utf-8")
    code, out = run(capsys, "ingest", FEISHU + "?from=wiki")
    assert code == 0 and out["submissions"][0]["state"] == "ready"
    assert out["submissions"][0]["detail"] == ("lark-cli could not read it; the maintainer reads it as the wiki's "
                                               "Feishu app")
    fake.unlink()
    code, out = run(capsys, "ingest", "https://example.com/post")  # not Feishu: never handed to lark-cli
    assert code == 0 and out["submissions"][0]["state"] == "needs_access"
    assert out["submissions"][0]["detail"].startswith("nothing reads this link for you")
    code, out = run(capsys, "ingest", FEISHU + "?v=2")
    assert out["submissions"][0]["detail"].startswith("lark-cli is not installed here;")

    # A bundle that takes no links: refused before anything is sent, the file ahead of it too.
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n\nsent first\n", encoding="utf-8")
    jobs = set((bundle(gate) / ".okf" / "jobs").glob("*.json"))
    for gate_env in ({"AIWIKI_INTAKE": "inbox", "AIWIKI_CHANGESETS_COMMIT": "kb-b"}, {}):
        gate.app(**gate_env)
        capsys.readouterr()
        with pytest.raises(SystemExit) as refused:
            cli.main(["ingest", str(notes), "https://example.com/other"])
        assert refused.value.code == 2 and "bundle kb-a takes content, not links" in capsys.readouterr().out
    assert set((bundle(gate) / ".okf" / "jobs").glob("*.json")) == jobs


def test_member_uploads_keep_binary_evidence_verbatim(gate) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    receipt = ingest(gate, content_b64=base64.b64encode(png).decode(), filename="Funnel Chart.PNG").json()
    item = M.get_item(bundle(gate), receipt["item"])
    assert receipt["status"] == "ready" and item["origin"]["filename"] == "Funnel Chart.PNG"
    assert M.read_file(bundle(gate), item["id"], "source.PNG") == png and item["origin"]["redactions"] == 0
    assert git(bundle(gate), "status", "--porcelain") == ""  # sources/inbox and .okf stay out of Git
    path = receipt["intake"]["path"]  # but for the copy intake commits, as sent
    assert path.startswith("sources/inbox/intake/funnel-chart-") and path.endswith(".png")
    assert subprocess.run(["git", "-C", str(gate.remote), "show", f"main:{path}"], capture_output=True,
                          check=True).stdout == png


def test_the_maintainer_reads_a_member_link_as_the_wiki_app(gate, tmp_path, monkeypatch, capsys) -> None:
    """A Feishu link sent alone: ``maint next`` reads it with lark-cli --as bot and freezes it beside
    the link, or closes it needs_access and serves the next item (design §6 step 2)."""
    config, st, log = maintainer_host(tmp_path, monkeypatch)
    private = ingest(gate, url=FEISHU + "?private").json()
    gate.connect("curator")

    code, begun = run(capsys, "maint", "begin", "--run", "WAIO-7", "--only", "inbox", "--max-items", "3",
                      "--config", config, "--state-dir", st)
    assert code == 0 and begun["collect"] == {"inbox": {"status": "ok", "ready": 1}}, begun
    code, empty = run(capsys, "maint", "next", "--state-dir", st)
    assert code == 10 and empty["closed"] == [{"item": private["item"], "status": "needs_access"}], empty
    followed = job(gate, private["id"])
    assert followed["status"] == "needs_access" and followed["reason"].startswith(
        "the wiki's Feishu app cannot read it (lark-cli could not read it): ingest the link where lark-cli")

    shared = ingest(gate, url="https://example.feishu.cn/docx/doxcnOK").json()
    code, brief = run(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and brief["item"] == shared["item"] and "closed" not in brief, brief
    assert [file["name"] for file in brief["files"]] == ["link.txt", "source.md"]
    assert log.read_text(encoding="utf-8").split()[-8:] == [
        "docs", "+fetch", "--doc", "https://example.feishu.cn/docx/doxcnOK", "--doc-format", "markdown", "--as", "bot"]
    item = M.get_item(bundle(gate), shared["item"])
    frozen = M.read_file(bundle(gate), item["id"], "source.md").decode()
    assert frozen.startswith("# Retry cap") and FAKE_KEY not in frozen
    assert item["files"][1]["origin"]["fetched"] == {"tool": "lark-cli", "as": "bot", "document_id": "doxcnOK",
                                                     "revision_id": 3}
    parts = [changeset.EvidenceFile(f"{item['id']}/{file['name']}", M.read_file(bundle(gate), item["id"], file["name"]),
                                    file["origin"]) for file in item["files"]]
    packet, errors = changeset.build_packet({"id": "retry-cap-2026-09-28", "item_files": [p.name for p in parts]},
                                            parts)
    assert errors == [] and b"doxcnOK" in packet.data  # both parts, text: one packet the gate takes
    run(capsys, "maint", "end", "--run", "WAIO-7", "--state-dir", st)


def test_an_unread_member_link_is_retried_not_lost(gate, tmp_path, monkeypatch, capsys) -> None:
    """A lark-cli timeout parks the link for the next run (transient, a slow network says nothing
    about access); a link closed needs_access reopens when its member sends it again, as the
    reason tells them to once the wiki's app can read it."""
    config, st, _log = maintainer_host(tmp_path, monkeypatch)
    slow = ingest(gate, url=FEISHU + "?slow").json()
    private = ingest(gate, url=FEISHU + "?private").json()
    gate.connect("curator")
    real_run = subprocess.run

    def slow_run(args, *rest, **kwargs):
        if Path(str(args[0])).name == "lark-cli" and "slow" in " ".join(map(str, args)):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_run)
    code, _begun = run(capsys, "maint", "begin", "--run", "WAIO-8", "--only", "inbox", "--config", config,
                        "--state-dir", st)
    assert code == 0
    code, empty = run(capsys, "maint", "next", "--state-dir", st)
    assert code == 10 and sorted(empty["closed"], key=lambda row: row["status"]) == [
        {"item": private["item"], "status": "needs_access"}, {"item": slow["item"], "status": "parked"}], empty
    parked = M.get_item(bundle(gate), slow["item"])
    assert parked["attempts"]["counted"] == 0 and parked["attempts"]["history"][-1]["class"] == "transient"
    assert job(gate, slow["id"])["status"] == "parked"

    # The member shared the doc with the app and sends the link again: the same job, reopened.
    again = ingest(gate, url=FEISHU + "?private").json()
    assert (again["deduplicated"], again["id"], again["item"], again["status"]) == (
        False, private["id"], private["item"], "ready")
    assert M.get_item(bundle(gate), private["item"])["reopened"][-1]["reason"] == "resubmitted"
    assert ingest(gate, url=FEISHU + "?slow").json()["deduplicated"] is True  # parked: it comes back by itself
    run(capsys, "maint", "end", "--run", "WAIO-8", "--state-dir", st)
    assert run(capsys, "maint", "begin", "--run", "WAIO-9", "--only", "inbox", "--config", config,
               "--state-dir", st)[0] == 0
    assert M.get_item(bundle(gate), slow["item"])["status"] == "ready"
    run(capsys, "maint", "end", "--run", "WAIO-9", "--state-dir", st)


def test_a_resubmission_relinks_an_item_whose_job_was_lost(gate, monkeypatch) -> None:
    """The item follows the submission's job: after a requeue whose Codex job failed, or a job
    write that failed after its item's, the same content again gets a live item."""
    text = "# Retry cap\n\nWe cap retries at 3.\n"
    receipt = ingest(gate, text=text).json()
    assert [row["item"] for row in requeue(gate).json()["requeued"]] == [receipt["item"]]
    worker._q.join()
    failed = I.read_job(bundle(gate), receipt["id"])
    failed.update(status="failed", phase="rolled_back")
    I._write_atomic(I.job_path(bundle(gate), receipt["id"]), failed)

    again = ingest(gate, text=text).json()
    assert again["deduplicated"] is False and again["item"] == receipt["item"] and again["status"] == "ready"
    item = M.get_item(bundle(gate), receipt["item"])
    assert item["origin"]["job"] == again["id"] and item["reopened"][-1]["from"] == "requeued"
    assert ingest(gate, text=text).json()["id"] == again["id"]

    writer = I._write_atomic

    def disk_full(path: Path, value: dict) -> None:
        if value.get("mode") == "inbox":
            raise OSError(28, "No space left on device")
        writer(path, value)

    monkeypatch.setattr(I, "_write_atomic", disk_full)
    with pytest.raises(OSError):
        ingest(gate, text="# Disk\n\nfull\n")
    monkeypatch.setattr(I, "_write_atomic", writer)
    retried = ingest(gate, text="# Disk\n\nfull\n").json()
    assert M.get_item(bundle(gate), retried["item"])["origin"]["job"] == retried["id"]
    assert {row["item"] for row in requeue(gate).json()["requeued"]} == {receipt["item"], retried["item"]}


def test_text_larger_than_one_evidence_packet_is_refused(gate, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_CHANGESET_MAX_PACKET_TEXT_BYTES", "4096")
    big = "# Big\n\n" + "one fact per line\n" * 300

    refused = ingest(gate, text=big)

    assert refused.status_code == 413 and "split it into parts and ingest each" in refused.json()["detail"]
    assert not list((bundle(gate) / "sources" / "inbox").glob("*")) and M.list_items(bundle(gate))["total"] == 0
    assert ingest(gate, text=big[:4000]).json()["status"] == "ready"
    drop = bundle(gate) / "sources" / "inbox" / "big.md.source"
    drop.write_text(big, encoding="utf-8")
    assert worker.sweep_once([bundle(gate)]) == 1
    [dropped] = M.list_items(bundle(gate), status="needs_conversion")["items"]
    assert dropped["origin"]["via"] == "drop" and "split it" in dropped["resolution"]["reason"]


def test_submissions_per_day_bound_what_one_principal_queues(gate, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_SUBMISSIONS_PER_DAY", "2")
    for n in range(2):
        assert ingest(gate, text=f"# Note {n}\n").status_code == 200

    over = ingest(gate, text="# Note 2\n")

    assert over.status_code == 429 and "submissions_per_day quota of 2" in over.json()["detail"]
    assert 0 < int(over.headers["Retry-After"]) <= 86400
    assert ingest(gate, text="# Note 0\n").json()["deduplicated"] is True  # a resend is no new submission
    assert ingest(gate, "owner", text="# Note 2\n").status_code == 200  # each principal has its own
    now = M._now
    monkeypatch.setattr(M, "_now", lambda: now() + timedelta(days=1, minutes=1))
    assert ingest(gate, text="# Note 3\n").status_code == 200


def test_requeue_takes_back_a_dead_runs_item_and_accounts_for_every_item(gate, monkeypatch, capsys) -> None:
    for n in range(4):
        ingest(gate, text=f"# Note {n}\n\nfact {n}\n")
    link = ingest(gate, url=FEISHU).json()["item"]
    dead = claim(gate, "WAIO-1")["id"]  # its run dies; three hours on its lease is gone
    rest = sorted({row["id"] for row in M.list_items(bundle(gate), origin="member")["items"]} - {dead, link})
    stuck, lost = rest[:2]
    edit(gate, stuck, status="needs_human", resolution={"outcome": "needs_human", "reason": "attempt_cap"})
    I.job_path(bundle(gate), M.get_item(bundle(gate), lost)["origin"]["job"]).unlink()
    now = M._now
    monkeypatch.setattr(M, "_now", lambda: now() + timedelta(hours=4))

    result = requeue(gate, reason="rollback").json()

    assert {row["item"] for row in result["requeued"]} == {dead, stuck, rest[2]} and result["held"] == []
    assert {row["item"]: row["error"] for row in result["unavailable"]} == {
        lost: "its job record is missing or invalid",
        link: "a link with no stored source: Codex cannot take it; close it with POST /admin/items/<id>/resolve"}
    history = M.get_item(bundle(gate), dead)["attempts"]["history"]
    assert history[-1]["class"] == "interrupted" and M.get_item(bundle(gate), dead)["status"] == "requeued"

    for item_id in (link, lost):  # what Codex cannot take, the owner closes by hand
        closing = gate.client.post(f"/admin/items/{item_id}/resolve", params={"bundle": "kb-a"},
                                   headers=gate.headers("owner"),
                                   json={"outcome": "needs_access", "reason": "rollback"})
        assert closing.status_code == 200, closing.text

    # One the live run claims while the requeue runs is held by it, not dropped; unknown ids say so.
    late = ingest(gate, text="# Late\n\nlate fact\n").json()["item"]
    real = M.requeue

    def racing(path, ids, **kwargs):
        M.acquire_lease(path, "maintainer", principal="process:ai-wiki-maintainer", run="WAIO-2")
        M.next_item(path, principal="process:ai-wiki-maintainer", run="WAIO-2")
        return real(path, ids, **kwargs)

    monkeypatch.setattr(M, "requeue", racing)
    gate.connect("owner")
    capsys.readouterr()
    code = cli.main(["admin", "inbox", "requeue", "--item", late, "--item", "it_000000000000", "--json"])
    answer = json.loads(capsys.readouterr().out)
    assert code == 1 and answer["held"] == [late] and answer["requeued"] == [], answer
    assert answer["unavailable"] == [{"item": "it_000000000000", "error": "no such member item"}]


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


def edit(gate: Gate, item_id: str, **fields) -> None:
    record = bundle(gate) / ".okf" / "maint" / "items" / item_id / "item.json"
    record.write_text(json.dumps({**json.loads(record.read_text(encoding="utf-8")), **fields}), encoding="utf-8")
