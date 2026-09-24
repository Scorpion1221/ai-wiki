"""CLI curation verbs against the writer gate (design §3, acceptance §13 W7).

``doctor --role``, ``workspace pull/status/diff``, ``concept new``, ``validate`` and
``propose``: the CLI runs in-process and reaches the real service app through
``Gate.connect``, so each verb meets the gate's actual answers.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from gate_fixture import CURATOR, EVIDENCE, EVIDENCE_ID, METRIC, TOKENS, Gate, cite, cited, wait_for

from aiwiki.cli import doctor, workspace
from aiwiki.cli import main as cli
from aiwiki.service import maint_state as M
from aiwiki.service import worker

VIEWS = "metrics/view-references-exposure-proxy-2026-09.md"
PROBE = "metrics/probe-funnel.md"
SKILLS = Path(__file__).resolve().parents[1] / "skills"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    gate.calls = gate.connect()
    monkeypatch.setattr(workspace, "RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(workspace, "POLL_S", 0.02)
    yield gate
    gate.close()


@pytest.fixture
def operator(gate) -> None:
    """Connect as the owner's curate-only token: only a human proposes an uploaded packet."""
    gate.calls = gate.connect("operator")


@pytest.fixture
def ws(gate, tmp_path, capsys) -> Path:
    """A freshly pulled workspace of kb-a, with the changeset's evidence file beside it."""
    (tmp_path / "status.md").write_bytes(EVIDENCE)
    assert wiki(capsys, "workspace", "pull", "--dir", tmp_path / "ws")[0] == 0
    return tmp_path / "ws"


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


def upload(ws: Path) -> list:
    return ["--dir", ws, "--upload", ws.parent / "status.md", "--source-id", EVIDENCE_ID]


def state(ws: Path) -> dict:
    return json.loads((ws / ".ai-wiki" / "workspace.json").read_text(encoding="utf-8"))


def new_concept(capsys, ws: Path) -> None:
    """``concept new`` for PROBE, and a cited body."""
    code, out = wiki(capsys, "concept", "new", PROBE, "--dir", ws, "--type", "Metric", "--title", "Probe funnel",
                     "--description", "A probe: of the funnel", "--tags", "metric,funnel", "--source-id", EVIDENCE_ID)
    assert code == 0, out
    with (ws / PROBE).open("a", encoding="utf-8") as concept:
        concept.write(f"The funnel moved.[^{EVIDENCE_ID}]\n\n[^{EVIDENCE_ID}]: status file\n")


# --- doctor ------------------------------------------------------------------------------------


def test_doctor_passes_a_curator_with_exactly_its_scopes(gate, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    code, report = wiki_json(capsys, "doctor", "--role", "curator", "--state-dir", tmp_path / "st",
                             "--skills-dir", SKILLS)

    assert code == 0, report
    checks = {row["check"]: row for row in report["checks"]}
    assert {"api", "scopes", "okf_version", "state_dir", "disk", "tool:multica", "skill:ai-wiki-maintainer"} <= set(
        checks)
    assert checks["skill:ai-wiki"]["detail"] == f"sha256 {doctor.skill_digest(SKILLS / 'ai-wiki')}"


def test_doctor_fails_closed(gate, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None if tool == "multica" else f"/usr/bin/{tool}")
    st = tmp_path / "st"

    def failed(*args) -> set[str]:
        code, report = wiki_json(capsys, "doctor", *args, "--state-dir", st)
        assert code == 4 and report["ok"] is False
        return {row["check"] for row in report["checks"] if not row["ok"]}

    assert failed("--role", "auditor") == {"scopes"}  # the curator's token is not an auditor's
    assert failed("--role", "curator") == {"tool:multica"}
    monkeypatch.setenv("AIWIKI_TOKEN", TOKENS["actorless"])  # curate scopes, but nothing to stamp
    assert failed("--role", "curator") == {"actor", "tool:multica"}
    monkeypatch.setenv("AIWIKI_TOKEN", TOKENS["owner"])
    assert failed("--role", "curator") >= {"scopes"}  # extra scopes fail too: no agent loop runs as admin
    monkeypatch.setenv("AIWIKI_TOKEN", TOKENS["operator"])  # the owner's curate-only token (design §7)
    assert failed("--role", "curator") == {"tool:multica"}  # passes but for this test's missing multica
    monkeypatch.setenv("AIWIKI_TOKEN", "aiw_x_unknown")
    assert failed("--role", "member") == {"whoami", "okf_version"}
    monkeypatch.delenv("AIWIKI_TOKEN")
    monkeypatch.setattr(gate.appmod, "CLIENT_MIN", "9.0.0")
    monkeypatch.setattr(doctor, "MIN_FREE_BYTES", 1 << 62)
    assert failed("--role", "auditor") == {"api", "scopes", "disk"}
    assert failed("--role", "member", "--skills-dir", tmp_path) == {"api", "disk", "scopes", "skill:ai-wiki"}
    toon_code, toon = wiki(capsys, "doctor", "--role", "member", "--state-dir", st)
    assert toon_code == 4 and "ok: false" in toon
    monkeypatch.setattr(cli, "CONFIG", tmp_path / "unconfigured.json")  # a fresh host: no endpoint, no token
    assert failed("--role", "member") == {"config", "disk"}


def test_doctor_fails_a_run_on_a_whoami_the_read_mirror_answered(gate, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(gate.appmod, "CURATE_ON", False)  # the mirror: AIWIKI_CURATE=off

    code, report = wiki_json(capsys, "doctor", "--role", "curator", "--state-dir", tmp_path / "st")

    assert code == 4 and [row["check"] for row in report["checks"] if not row["ok"]] == ["writer"]
    monkeypatch.setenv("AIWIKI_TOKEN", TOKENS["member"])  # a member only ingests: /ingest is routed apart
    assert wiki_json(capsys, "doctor", "--role", "member", "--state-dir", tmp_path / "st")[0] == 0


# --- the HTTP client ---------------------------------------------------------------------------


def test_http_returns_every_status_and_raises_only_on_the_network(tmp_path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/truncated":  # the connection drops mid-body, as a restarting proxy's
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"{\"id\": ")
                self.close_connection = True
                return
            status = 304 if self.headers.get("If-None-Match") else 503
            self.send_response(status)
            self.send_header("X-AIWiki-Revision", "abc")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}", "token": "t"}))
        monkeypatch.setattr(cli, "CONFIG", config)
        try:
            unmodified = cli._http("GET", "/workspace", headers={"If-None-Match": '"abc"'})
            busy = cli._http("GET", "/workspace", bundle="kb")
            with pytest.raises(OSError, match="IncompleteRead"):
                cli._http("GET", "/truncated")
        finally:
            server.shutdown()
    assert (unmodified[0], unmodified[1]["x-aiwiki-revision"]) == (304, "abc")
    assert busy[0] == 503
    config.write_text(json.dumps({"endpoint": "http://127.0.0.1:9", "token": "t"}))
    with pytest.raises(OSError):
        cli._http("GET", "/whoami", timeout=2)


# --- workspace pull / status / diff ---------------------------------------------------------


def test_pull_writes_the_published_tree_and_a_second_pull_is_a_304(gate, ws, capsys) -> None:
    saved = state(ws)
    assert (saved["bundle"], saved["base_revision"], saved["actor"]) == ("kb-a", gate.head(), CURATOR)
    assert (ws / METRIC).read_bytes() == (gate.writer / METRIC).read_bytes()
    assert saved["hashes"][METRIC] == hashlib.sha256((ws / METRIC).read_bytes()).hexdigest()
    assert any((ws / "sources").iterdir()) and not (ws / "viz.html").exists()
    assert (ws / ".ai-wiki" / "base" / METRIC).read_bytes() == (ws / METRIC).read_bytes()

    code, again = wiki_json(capsys, "workspace", "pull", "--dir", ws)

    assert code == 0 and again["up_to_date"] is True and again["updated"] == []
    assert gate.calls[-1][1:3] == ("/workspace", 304)
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1]["changes"] == []
    # Never over a directory that is not a workspace, nor a workspace of another bundle.
    assert wiki(capsys, "workspace", "pull", "--dir", ws / "metrics")[0] == 2
    assert wiki(capsys, "-b", "kb-b", "workspace", "pull", "--dir", ws)[0] == 2


def test_pull_keeps_local_edits_and_sets_aside_the_ones_the_server_changed(gate, ws, capsys) -> None:
    mine = cited(gate.read(METRIC), "My own claim.")
    (ws / METRIC).write_text(mine, encoding="utf-8")
    (ws / VIEWS).write_text(gate.read(VIEWS) + "\nLocal note.\n", encoding="utf-8")
    committed = gate.post(gate.request()).json()  # another writer changes METRIC first
    assert committed["status"] == "done"

    code, pulled = wiki_json(capsys, "workspace", "pull", "--dir", ws)

    assert code == 7 and pulled["conflicts"] == [METRIC] and pulled["kept"] == [VIEWS]
    assert pulled["base_revision"] == committed["commit"] == state(ws)["base_revision"]
    assert (ws / METRIC).read_bytes() == (gate.writer / METRIC).read_bytes()
    assert (ws / f"{METRIC}.mine").read_text(encoding="utf-8") == mine
    assert (ws / VIEWS).read_text(encoding="utf-8").endswith("\nLocal note.\n")
    assert (ws / committed["source_snapshot"]).is_file()  # the committed packet arrived too
    status = wiki_json(capsys, "workspace", "status", "--dir", ws)[1]
    assert status["changes"] == [{"path": VIEWS, "change": "modified"}] and status["conflicts"] == [METRIC]


def test_a_first_pull_that_fails_can_be_pulled_again(gate, tmp_path, capsys, monkeypatch) -> None:
    connected, ws = cli._http, tmp_path / "ws"

    def whoami_down(method, route, **kwargs):
        if route == "/whoami":
            raise urllib.error.URLError("connection reset")
        return connected(method, route, **kwargs)

    monkeypatch.setattr(cli, "_http", whoami_down)
    assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 1
    assert not ws.exists()  # the actor is asked before anything is written
    monkeypatch.setattr(cli, "_http", connected)
    # A first pull cut short once it had started writing (a full disk, a kill) is pulled into afresh.
    (ws / ".ai-wiki" / "incoming").mkdir(parents=True)
    (ws / METRIC).parent.mkdir(parents=True)
    (ws / METRIC).write_text("half a file", encoding="utf-8")

    assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    assert (ws / METRIC).read_bytes() == (gate.writer / METRIC).read_bytes()
    assert state(ws)["actor"] == CURATOR and not (ws / ".ai-wiki" / "incoming").exists()


def test_pull_downloads_the_tree_again_when_its_base_is_damaged(gate, ws, capsys) -> None:
    mine = cited(gate.read(METRIC), "My own claim.")
    (ws / METRIC).write_text(mine, encoding="utf-8")
    shutil.rmtree(ws / ".ai-wiki" / "base")  # say, to keep rg from matching everything twice

    code, pulled = wiki_json(capsys, "workspace", "pull", "--dir", ws)

    assert code == 0 and pulled["kept"] == [METRIC] and pulled["conflicts"] == []
    assert [call[2] for call in gate.calls if call[1] == "/workspace"][-1] == 200  # not a 304
    assert (ws / ".ai-wiki" / "base" / VIEWS).read_bytes() == (ws / VIEWS).read_bytes() == (
        gate.writer / VIEWS).read_bytes()
    assert (ws / METRIC).read_text(encoding="utf-8") == mine
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1]["changes"] == [
        {"path": METRIC, "change": "modified"}]


def test_diff_shows_local_edits_and_the_stamped_bytes(gate, ws, capsys) -> None:
    assert wiki(capsys, "workspace", "diff", "--dir", ws) == (0, "")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    code, plain = wiki(capsys, "workspace", "diff", "--dir", ws)
    stamped_code, stamped = wiki(capsys, "workspace", "diff", "--stamped", *upload(ws))

    assert code == 0 and plain.startswith(f"--- a/{METRIC}\n+++ b/{METRIC}\n")
    assert f"+- {{id: {EVIDENCE_ID}, resource: evidence:packet}}" in plain
    assert stamped_code == 0 and f"by: {CURATOR}" in stamped and "resource: /sources/" in stamped
    assert "evidence:packet" not in stamped


# --- concept new -------------------------------------------------------------------------------


def test_concept_new_writes_a_skeleton_without_service_keys(gate, ws, capsys) -> None:
    new_concept(capsys, ws)

    text = (ws / PROBE).read_text(encoding="utf-8")
    assert text.startswith("---\ntype: Metric\ntitle: Probe funnel\ndescription: 'A probe: of the funnel'\n")
    assert f"- id: {EVIDENCE_ID}\n  resource: evidence:packet\n---\n# Summary\n" in text
    assert not {"status:", "generated:", "verified:"} & {line.split(" ")[0] for line in text.splitlines()}
    code, verdict = wiki_json(capsys, "validate", *upload(ws))
    assert code == 0 and verdict["status"] == "would_apply", verdict
    for refused in (PROBE, "SCHEMA.md", "sources/x.md", "metrics/notes.txt"):
        code, out = wiki(capsys, "concept", "new", refused, "--dir", ws, "--type", "Metric", "--title", "X",
                         "--description", "D", "--tags", "a", "--source-id", EVIDENCE_ID)
        assert code == 2, (refused, out)


# --- validate ----------------------------------------------------------------------------------


def test_validate_answers_in_the_gates_422_format(gate, ws, capsys) -> None:
    (ws / VIEWS).write_text(gate.read(VIEWS) + "\nAn uncited claim.\n", encoding="utf-8")
    broken = gate.read(METRIC).replace("title: Redacted title", "title: Redacted: title")
    (ws / METRIC).write_text(broken, encoding="utf-8")

    code, verdict = wiki_json(capsys, "validate", *upload(ws))

    assert code == 6 and (verdict["status"], verdict["http_status"]) == ("rejected", 422)
    assert verdict["failure"]["class"] == "model_output" and "files" not in verdict
    by_path = {error["path"]: error for error in verdict["errors"]}
    assert by_path[VIEWS]["code"] == "uncited_change" and by_path[VIEWS]["hint"]
    assert (by_path[METRIC]["code"], by_path[METRIC]["line"]) == ("yaml_parse", 3)
    # Without evidence options a placeholder packet stands in, named by the id the edits cite.
    (ws / VIEWS).write_bytes((ws / ".ai-wiki" / "base" / VIEWS).read_bytes())
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    assert wiki_json(capsys, "validate", "--dir", ws)[1]["status"] == "would_apply"
    toon_code, toon = wiki(capsys, "validate", "--dir", ws)
    assert toon_code == 0 and 'status: "would_apply"' in toon


def test_an_evidence_file_the_item_does_not_hold_intact_is_refused(gate, ws, capsys, monkeypatch) -> None:
    item_id = gate.item(run="WAIO-1")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    cache = ws / ".ai-wiki" / "items" / item_id / "S1-status.md"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"tampered")  # a damaged cache is fetched again
    assert wiki(capsys, "validate", "--dir", ws, "--item", item_id)[0] == 0
    assert cache.read_bytes() == EVIDENCE
    connected = cli._http
    monkeypatch.setattr(cli, "_http", lambda method, route, **kw: (200, {}, b"other bytes")
                        if "/files/" in route else connected(method, route, **kw))
    cache.write_bytes(b"tampered")
    code, out = wiki(capsys, "validate", "--dir", ws, "--item", item_id)
    assert code == 1 and "could not be fetched intact" in out


def test_an_actorless_token_is_refused_locally_as_the_writer_refuses_it(gate, tmp_path, capsys) -> None:
    calls = gate.connect(token="actorless")  # member:legacy-eb17 holds curate but stamps nothing
    (tmp_path / "status.md").write_bytes(EVIDENCE)
    ws = tmp_path / "ws"
    assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    assert state(ws)["actor"] is None
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    for verb in (("validate",), ("workspace", "diff", "--stamped"), ("propose", "--state-dir", tmp_path / "st"),
                 ("propose", "--dry-run", "--state-dir", tmp_path / "st")):
        code, out = wiki(capsys, *verb, *upload(ws))
        assert code == 4 and "no actor" in out, (verb, out)
    assert [call[2] for call in calls if call[1] == "/changesets"] == [403]  # only the dry-run asked


def test_a_concept_that_is_not_utf8_is_refused_not_rewritten(gate, ws, capsys, tmp_path) -> None:
    (ws / METRIC).write_bytes(cited(gate.read(METRIC), "Cafe moved.").encode().replace(b"Cafe", b"Caf\xe9"))

    code, out = wiki(capsys, "validate", *upload(ws))
    proposed, _out = wiki(capsys, "propose", *upload(ws), "--state-dir", tmp_path / "st")

    assert code == 6 and f"{METRIC} is not UTF-8" in out
    assert proposed == 6 and not [call for call in gate.calls if call[1] == "/changesets"]


# --- propose -----------------------------------------------------------------------------------


def test_propose_commits_and_refreshes_the_workspace(gate, operator, ws, capsys, tmp_path) -> None:
    new_concept(capsys, ws)
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    code, receipt = wiki_json(capsys, "propose", *upload(ws), "--run", "WAIO-612", "--state-dir", tmp_path / "st")

    assert code == 0, receipt
    assert (receipt["status"], receipt["run"], receipt["commit"]) == ("done", "WAIO-612", gate.remote_head())
    assert receipt["concept_files"] == [METRIC, PROBE]
    assert receipt["workspace"]["base_revision"] == receipt["commit"] == state(ws)["base_revision"]
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1] | {"base_revision": None} == {
        "bundle": "kb-a", "base_revision": None, "changes": [], "conflicts": []}
    assert (ws / PROBE).read_bytes() == (gate.writer / PROBE).read_bytes()  # the stamped, committed bytes
    assert "status: draft" in (ws / PROBE).read_text(encoding="utf-8")


def test_propose_sends_nothing_the_local_gate_rejects(gate, ws, capsys, tmp_path) -> None:
    (ws / VIEWS).write_text(gate.read(VIEWS) + "\nAn uncited claim.\n", encoding="utf-8")
    head = gate.head()

    code, verdict = wiki_json(capsys, "propose", *upload(ws), "--state-dir", tmp_path / "st")

    assert code == 6 and verdict["errors"][0]["code"] == "uncited_change"
    assert not [call for call in gate.calls if call[1] == "/changesets"]
    gate.assert_untouched(head)
    assert gate.jobs() == []


def test_propose_dry_run_asks_the_writer_and_writes_nothing(gate, operator, ws, capsys, tmp_path) -> None:
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    head = gate.head()

    code, verdict = wiki_json(capsys, "propose", *upload(ws), "--dry-run", "--state-dir", tmp_path / "st")
    (ws / VIEWS).write_text(gate.read(VIEWS) + "\nAn uncited claim.\n", encoding="utf-8")
    refused, rejection = wiki_json(capsys, "propose", *upload(ws), "--dry-run", "--state-dir", tmp_path / "st")

    assert code == 0 and (verdict["dry_run"], verdict["status"], verdict["published_revision"]) == (
        True, "would_apply", head)
    assert refused == 6 and rejection["dry_run"] is True  # the writer's verdict, not the local one
    assert {error["code"] for error in rejection["errors"]} == {"uncited_change"}
    gate.assert_untouched(head)
    assert gate.jobs() == [] and not (tmp_path / "st").exists()


def test_propose_an_item_closes_it_with_its_frozen_evidence(gate, ws, capsys, tmp_path) -> None:
    item_id = gate.item(run="WAIO-612")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    assert wiki(capsys, "validate", "--dir", ws, "--item", item_id)[0] == 0
    # As the design's prompt calls it (§4.10): no --run, so the run that claimed the item proposes.
    code, receipt = wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", tmp_path / "st")

    assert code == 0, receipt
    assert receipt["run"] == "WAIO-612"
    assert receipt["closed_items"] == [item_id] and receipt["work_items"] == [item_id]
    assert receipt["evidence"]["id"] == EVIDENCE_ID  # the id the concept cites
    assert M.get_item(gate.writer, item_id)["status"] == "curated"
    assert (ws / ".ai-wiki" / "items" / item_id / "S1-status.md").read_bytes() == EVIDENCE
    assert (gate.writer / receipt["source_snapshot"]).read_bytes() == EVIDENCE


def test_propose_follows_a_202_to_its_receipt(gate, operator, ws, capsys, tmp_path) -> None:
    gate.app(AIWIKI_CHANGESET_WAIT_S="0.1")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    release = threading.Event()

    def codex_pass() -> None:
        with worker.serialized_mutation():
            release.wait(10)

    holder = threading.Thread(target=codex_pass)
    holder.start()
    wait_for(worker.is_mutating)
    threading.Timer(0.5, release.set).start()
    try:
        code, receipt = wiki_json(capsys, "propose", *upload(ws), "--state-dir", tmp_path / "st")
    finally:
        release.set()
        holder.join()

    assert code == 0, receipt
    assert receipt["status"] == "done" and receipt["commit"] == gate.remote_head()
    assert ("POST", "/changesets", 202) in [call[:3] for call in gate.calls]
    assert [call[:3] for call in gate.calls if call[1].startswith("/jobs/")][-1][2] == 200


def test_propose_resends_the_same_bytes_parks_and_reverts_the_item_when_no_answer_comes(
        gate, ws, capsys, tmp_path) -> None:
    item_id = gate.item(run="WAIO-1")
    edit = cited(gate.read(METRIC))
    (ws / METRIC).write_text(edit, encoding="utf-8")
    (ws / f"{METRIC}.mine").write_text("the item's earlier version\n", encoding="utf-8")
    connected = cli._http
    # A reset, the writer's transient receipt, then what Cloudflare answers while the writer restarts.
    outage = [None, (503, b'{"id": "j1", "status": "failed", "failure": {"class": "transient"}}'),
              (502, b"<html>502 Bad Gateway</html>"), (524, b"<html>A timeout occurred</html>")]

    def flaky(method, route, **kwargs):
        if route == "/changesets" and outage:
            answer = outage.pop(0)
            if answer is None:
                raise urllib.error.URLError("connection reset")
            gate.calls.append((method, route, answer[0], kwargs.get("data")))
            return answer[0], {}, answer[1]
        return connected(method, route, **kwargs)

    gate.monkeypatch.setattr(cli, "_http", flaky)
    args = ("propose", "--dir", ws, "--item", item_id, "--run", "WAIO-1", "--state-dir", tmp_path / "st")

    code, result = wiki_json(capsys, *args)

    assert code == 8 and result["park"] == {"parked": True} and result["reverted"] == [METRIC]
    assert M.get_item(gate.writer, item_id)["status"] == "parked"
    sent = [call[3] for call in gate.calls if call[1] == "/changesets"]
    assert len(sent) == 3 and len(set(sent)) == 1  # the same bytes each time; the reset never reached it
    assert not (tmp_path / "st").exists()  # nothing is kept on disk for a resend
    # The parked item's edits are gone from the workspace, so the next item starts clean.
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1]["changes"] == []
    assert workspace.conflicts(ws) == [] and (ws / METRIC).read_text(encoding="utf-8") == gate.read(METRIC)

    (ws / METRIC).write_text(edit, encoding="utf-8")  # the item comes back; the same edit again
    outage.append((500, b"Internal Server Error"))  # no receipt: the job may exist, so resend
    code, receipt = wiki_json(capsys, *args)

    assert code == 0, receipt
    assert receipt["closed_items"] == [item_id] and receipt["commit"] == gate.remote_head()
    assert {call[3] for call in gate.calls if call[1] == "/changesets"} == {sent[0]}


def test_only_an_answer_that_settles_the_changeset_is_final() -> None:
    assert workspace._final(422, {"status": "rejected"}) and workspace._final(409, {})
    assert workspace._final(500, {"id": "j1", "status": "failed", "failure": {"class": "internal"}})
    assert not any(workspace._final(status, {"detail": "<html>Bad Gateway</html>"})
                   for status in (500, 502, 503, 504, 520, 521, 522, 523, 524))


def test_propose_reports_a_conflict_and_leaves_the_workspace_alone(gate, operator, ws, capsys, tmp_path) -> None:
    mine = cited(gate.read(METRIC), "My own claim.")
    (ws / METRIC).write_text(mine, encoding="utf-8")
    assert gate.post(gate.request()).status_code == 201  # the server's METRIC moves on

    code, rejection = wiki_json(capsys, "propose", *upload(ws), "--state-dir", tmp_path / "st")

    assert code == 7 and rejection["errors"][0]["code"] == "conflict"
    assert (ws / METRIC).read_text(encoding="utf-8") == mine



def test_a_retitle_is_proposed_with_allow_retype(gate, ws, capsys, tmp_path) -> None:
    # The gate's identity_locked hint names allow.retype; the maintainer's verbs can send it.
    item_id = gate.item(run="WAIO-1")
    (ws / METRIC).write_text(cited(gate.read(METRIC)).replace("title: Redacted title", "title: Renamed funnel", 1),
                             encoding="utf-8")
    allow = ["--allow-retype", f"{METRIC}:the funnel was renamed upstream"]

    locked, verdict = wiki_json(capsys, "validate", "--dir", ws, "--item", item_id)
    allowed = wiki_json(capsys, "validate", "--dir", ws, "--item", item_id, *allow)[0]
    code, receipt = wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, *allow, "--state-dir", tmp_path / "st")

    assert locked == 6 and [error["code"] for error in verdict["errors"]] == ["identity_locked"]
    assert "allow.retype" in verdict["errors"][0]["hint"]
    assert allowed == 0 and code == 0, receipt
    sent = json.loads([call[3] for call in gate.calls if call[1] == "/changesets"][-1])
    assert sent["allow"] == {"retype": [{"path": METRIC, "reason": "the funnel was renamed upstream"}]}
    assert "title: Renamed funnel" in gate.read(METRIC)

def test_propose_needs_evidence_and_something_to_propose(gate, ws, capsys, tmp_path) -> None:
    assert wiki(capsys, "propose", "--dir", ws)[0] == 2  # --item or --upload
    assert wiki(capsys, "propose", "--dir", ws, "--upload", ws.parent / "status.md")[0] == 2  # --source-id
    assert wiki(capsys, "propose", *upload(ws), "--state-dir", tmp_path / "st")[0] == 2  # nothing changed


def test_a_later_run_proposes_the_same_edit_with_its_own_request(gate, ws, capsys, tmp_path) -> None:
    item_id = gate.item(run="WAIO-1")
    edit = cited(gate.read(METRIC))
    (ws / METRIC).write_text(edit, encoding="utf-8")
    connected, down = cli._http, {"on": True}

    def unreachable(method, route, **kwargs):
        if route == "/changesets" and down["on"]:
            raise urllib.error.URLError("connection refused")
        return connected(method, route, **kwargs)

    gate.monkeypatch.setattr(cli, "_http", unreachable)
    args = ("propose", "--dir", ws, "--item", item_id, "--state-dir", tmp_path / "st")
    assert wiki_json(capsys, *args)[0] == 8 and gate.jobs() == []
    # The run ends; the next run takes the lease and is handed the same item again.
    assert gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"},
                              headers=gate.headers(run="WAIO-1")).status_code == 200
    headers = gate.headers(run="WAIO-2")
    assert gate.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=headers).status_code == 200
    assert gate.client.post("/maint/items/next", params={"bundle": "kb-a"}, headers=headers).json()["item"]["id"] \
        == item_id
    down["on"] = False
    (ws / METRIC).write_text(edit, encoding="utf-8")

    code, receipt = wiki_json(capsys, *args)

    assert code == 0, receipt  # its own request, not the WAIO-1 bytes the first attempt sent
    assert receipt["run"] == "WAIO-2" and receipt["closed_items"] == [item_id]


def test_propose_answers_0_once_committed_and_the_next_pull_takes_the_commit(gate, ws, capsys, tmp_path) -> None:
    item_id = gate.item(run="WAIO-1")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    (ws / VIEWS).write_text(cite(gate.read(VIEWS)), encoding="utf-8")
    connected = cli._http

    def reset_after_commit(method, route, **kwargs):
        if route == "/workspace" and any(call[1] == "/changesets" for call in gate.calls):
            raise urllib.error.URLError("connection reset")
        return connected(method, route, **kwargs)

    gate.monkeypatch.setattr(cli, "_http", reset_after_commit)
    code, receipt = wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", tmp_path / "st")

    assert code == 0 and receipt["closed_items"] == [item_id], receipt
    assert "connection reset" in receipt["workspace"]["error"] and sorted(state(ws)["discard"]) == [METRIC, VIEWS]
    later = cite(gate.read(VIEWS), claim="A later claim.")  # edited after the commit: kept, not discarded
    (ws / VIEWS).write_text(later, encoding="utf-8")
    gate.monkeypatch.setattr(cli, "_http", connected)
    code, pulled = wiki_json(capsys, "workspace", "pull", "--dir", ws)
    assert code == 7 and pulled["conflicts"] == [VIEWS] and METRIC in pulled["updated"]
    assert (ws / METRIC).read_bytes() == (gate.writer / METRIC).read_bytes() and "discard" not in state(ws)
    assert (ws / f"{VIEWS}.mine").read_text(encoding="utf-8") == later
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1]["changes"] == []


def test_a_committed_resolution_clears_its_mine(gate, operator, ws, capsys, tmp_path) -> None:
    (ws / METRIC).write_text(cited(gate.read(METRIC), "My own claim."), encoding="utf-8")
    assert gate.post(gate.request()).status_code == 201  # the server's METRIC moves on
    assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 7 and workspace.conflicts(ws) == [METRIC]
    # The agent re-applies its intent on the server's version (design §4.6).
    (ws / METRIC).write_text(cite((ws / METRIC).read_text(encoding="utf-8"), "funnel-status-2", "My own claim."),
                             encoding="utf-8")

    code, receipt = wiki_json(capsys, "propose", "--dir", ws, "--upload", ws.parent / "status.md",
                              "--source-id", "funnel-status-2", "--state-dir", tmp_path / "st")

    assert code == 0 and receipt["status"] == "done", receipt
    assert not (ws / f"{METRIC}.mine").exists()
    assert wiki_json(capsys, "workspace", "status", "--dir", ws)[1]["conflicts"] == []


def test_propose_stops_following_a_job_it_may_no_longer_see(gate, ws, capsys, tmp_path) -> None:
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")
    connected, polls, answer = cli._http, [], {}

    def queued(method, route, **kwargs):
        if route == "/changesets":
            return 202, {}, b'{"id": "abc123", "status": "queued"}'
        if route.startswith("/jobs/"):
            polls.append(route)
            return answer["status"], {}, b'{"detail": "refused"}'
        return connected(method, route, **kwargs)

    gate.monkeypatch.setattr(cli, "_http", queued)
    for status, exit_code in ((401, 4), (403, 4), (404, 1)):  # a revoked token (§8.5), a lost job
        answer["status"], polls[:] = status, []
        code, result = wiki_json(capsys, "propose", *upload(ws), "--wait", "5", "--state-dir", tmp_path / "st")
        assert (code, result["detail"], polls) == (exit_code, "refused", ["/jobs/abc123"])
