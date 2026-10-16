"""Attention work items (design §5.3, W18): the maintainer learns about concepts nobody is fixing.

A maintainer run start queues one item per concept whose current version the auditor judged
unverified, or whose effective freshness elapsed. The notice it carries is not evidence: the
gate refuses a changeset that cites it, and ``maint add-evidence source:`` freezes a source the
bundle already holds, at the bytes its ledger records.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from gate_fixture import AIO_AB, TODAY, Gate, cite, git

from aiwiki.cli import main as cli
from aiwiki.cli import maint
from aiwiki.maint import planner
from aiwiki.runtime import changeset
from aiwiki.service import attention
from aiwiki.service import maint_state as M

EPOCH = "2026-09-20T00:00:00Z"
CURATOR = "process:ai-wiki-maintainer"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH)
    yield gate
    gate.close()


def begin(gate: Gate, run: str) -> dict:
    response = gate.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run=run))
    assert response.status_code == 200, response.text
    return response.json()


def end(gate: Gate, run: str) -> None:
    gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run=run))


def unverify(gate: Gate, rel: str, note: str = "S1 says the lift was 3%, not 12%") -> dict:
    """The auditor reviews ``rel`` (in its backlog) and concludes unverified."""
    run = f"AUD-{len(gate.jobs())}"
    leased = gate.client.post("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", run))
    assert leased.status_code == 200, leased.text
    base = changeset.content_hash(git(gate.remote, "show", f"main:{rel}"))
    job = gate.post({"schema": changeset.SCHEMA, "kind": "audit", "base_revision": gate.head(), "reviews": [
        {"path": rel, "base": base, "verdict": "unverified", "note": note}]}, token="auditor", run=run).json()
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", run))
    assert job["status"] == "done" and job["reviews"][0]["outcome"] == "unverified", job
    return job


def regenerate(gate: Gate, rel: str, claim: str) -> None:
    """A curator's new version of ``rel``: one more claim citing an uploaded packet."""
    response = gate.post(gate.request(gate.put(rel, cite(gate.read(rel), claim=claim))))
    assert response.status_code == 201, response.text


def items(gate: Gate) -> list[dict]:
    return [item for item in M.list_items(gate.writer, limit=1000)["items"] if item["origin"]["kind"] == "attention"]


def notice(gate: Gate, item: dict) -> str:
    return M.read_file(gate.writer, item["id"], attention.NOTICE).decode("utf-8")


def ledger_entry(gate: Gate) -> tuple[str, bytes]:
    """One source the published bundle froze, and its bytes."""
    rel, sha = next(iter(attention.ledger(gate.writer).items()))
    data = (gate.writer / rel).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sha
    return rel, data


# --- the collector ----------------------------------------------------------------------------------


def test_an_unverified_review_of_the_current_version_queues_one_attention_item(gate) -> None:
    job = unverify(gate, AIO_AB)

    begun = begin(gate, "WAIO-1")

    assert begun["attention"] == {"status": "ok", "due": 1, "cap": M.ATTENTION_CAP, "created": 1, "merged": 0,
                                  "duplicate": 0, "deferred": 0, "invalid": 0, "by_reason": {"audit_unverified": 1}}
    assert {"interrupted", "unparked", "build_retry", "lease"} <= begun.keys()  # the fields it always had
    [item] = items(gate)
    base = changeset.content_hash(gate.read(AIO_AB))
    assert item["topic_key"] == f"attention:{AIO_AB}" and item["priority"] == planner.PRIORITY["attention"] == 45
    assert item["origin"] | {"note": None} == {
        "kind": "attention", "path": AIO_AB, "reason": "audit_unverified", "base": base, "review": job["id"],
        "reviewed_at": job["finished"], "downgrade": None, "note": None}
    assert [(f["name"], f["origin"]["kind"]) for f in item["files"]] == [("notice.md", "attention")]
    text = notice(gate, item)
    assert f"- review: {job['id']} at {job['finished']}" in text
    assert "> S1 says the lift was 3%, not 12%" in text and "maint add-evidence" in text
    cited = re.findall(r"resource: (/sources/\S+)", gate.read(AIO_AB))
    assert cited and all(f"- `{rel.lstrip('/')}`" in text for rel in cited)


def test_a_repeated_run_start_is_a_duplicate_and_a_new_version_a_new_item(gate) -> None:
    unverify(gate, AIO_AB)
    first = begin(gate, "WAIO-1")["attention"]
    again = begin(gate, "WAIO-1")["attention"]  # the same run renews: no new run, no collection
    end(gate, "WAIO-1")
    second = begin(gate, "WAIO-2")["attention"]

    assert first["created"] == 1 and again == {"status": "not_run", "reason": "the run already held the lease"}
    assert (second["due"], second["created"], second["duplicate"]) == (1, 0, 1), second
    [item] = items(gate)
    # The maintainer skips it: the same version never comes back.
    claimed = M.next_item(gate.writer, principal=CURATOR, run="WAIO-2")["item"]
    assert claimed["id"] == item["id"]
    M.resolve(gate.writer, item["id"], {"outcome": "skipped", "reason": "insufficient_evidence"},
              principal=CURATOR, run="WAIO-2")
    end(gate, "WAIO-2")
    assert begin(gate, "WAIO-3")["attention"]["duplicate"] == 1 and len(items(gate)) == 1
    end(gate, "WAIO-3")

    # A new version reviewed unverified again is a new item, under the same topic.
    regenerate(gate, AIO_AB, "The lift held in October.")
    unverify(gate, AIO_AB, note="still unsupported")
    assert begin(gate, "WAIO-4")["attention"]["created"] == 1
    new = [i for i in items(gate) if i["id"] != item["id"]]
    assert len(new) == 1 and new[0]["origin"]["base"] != item["origin"]["base"]
    assert "> still unsupported" in notice(gate, new[0])


def test_a_waiting_notice_merges_the_newer_one(gate) -> None:
    unverify(gate, AIO_AB)
    assert begin(gate, "WAIO-1")["attention"]["created"] == 1
    end(gate, "WAIO-1")
    [waiting] = items(gate)
    regenerate(gate, AIO_AB, "The lift held in October.")
    unverify(gate, AIO_AB, note="the new version is unsupported too")

    begun = begin(gate, "WAIO-2")["attention"]

    assert (begun["created"], begun["merged"]) == (0, 1), begun

    [merged] = items(gate)
    assert merged["id"] == waiting["id"] and merged["status"] == "ready" and len(merged["versions"]) == 1
    assert merged["origin"]["base"] == changeset.content_hash(gate.read(AIO_AB))
    assert "> the new version is unsupported too" in notice(gate, merged)


def test_only_a_review_of_the_current_bytes_counts(gate) -> None:
    unverify(gate, AIO_AB)
    regenerate(gate, AIO_AB, "The lift held in October.")

    begun = begin(gate, "WAIO-1")["attention"]

    assert (begun["due"], begun["created"]) == (0, 0) and items(gate) == []


def test_a_reverted_review_needs_no_attention(gate) -> None:
    job = unverify(gate, AIO_AB)
    reverted = gate.client.post("/admin/revert", params={"bundle": "kb-a"}, headers=gate.headers("owner"),
                                json={"changeset": job["id"], "reason": "the auditor's token leaked"})
    assert reverted.status_code == 201, reverted.text

    assert begin(gate, "WAIO-1")["attention"]["due"] == 0


def test_freshness_past_the_default_queues_a_refresh_item(gate, monkeypatch) -> None:
    # Generated 2026-09-08T08:44Z, the fixture's oldest: stale 90 days later, on 2026-12-07.
    visitors = "metrics/web-landing-new-visitor-distribution-2026-09.md"
    monkeypatch.setattr(attention, "today", lambda: date(2026, 12, 6))
    assert begin(gate, "WAIO-1")["attention"]["due"] == 0
    end(gate, "WAIO-1")
    monkeypatch.setattr(attention, "today", lambda: date(2026, 12, 7))

    begun = begin(gate, "WAIO-1")["attention"]

    [item] = items(gate)
    assert begun["by_reason"] == {"stale": 1} and item["origin"]["path"] == visitors
    assert item["priority"] == planner.PRIORITY["refresh"] == 20
    assert item["origin"] | {"base": None} == {"kind": "attention", "path": visitors, "reason": "stale",
                                               "base": None, "stale_on": "2026-12-07", "basis": "default"}
    assert "90 or more days old" in notice(gate, item) and "set `stale_after`" in notice(gate, item)


def test_a_deprecated_concept_needs_no_attention(gate, monkeypatch) -> None:
    deprecated = "metrics/view-references-exposure-proxy-2026-09.md"
    assert gate.post(gate.request(gate.deprecate(deprecated))).status_code == 201
    unverify(gate, AIO_AB)
    monkeypatch.setattr(attention, "today", lambda: TODAY + timedelta(days=400))  # every concept is stale

    begin(gate, "WAIO-1")

    queued = {item["origin"]["path"] for item in items(gate)}
    concepts = {rel for rel in git(gate.writer, "ls-files", "metrics", "experiments").splitlines()
                if rel.endswith(".md") and not rel.endswith("/index.md")}
    assert queued == concepts - {deprecated} and AIO_AB in queued


def test_an_unverified_review_outranks_elapsed_freshness(gate, monkeypatch) -> None:
    unverify(gate, AIO_AB)
    monkeypatch.setattr(attention, "today", lambda: TODAY + timedelta(days=400))  # every concept is stale

    begun = begin(gate, "WAIO-1")["attention"]

    reasons = {item["origin"]["path"]: item["origin"]["reason"] for item in items(gate)}
    assert reasons[AIO_AB] == "audit_unverified" and set(reasons.values()) == {"audit_unverified", "stale"}
    assert begun["created"] == len(reasons) == len(set(reasons))  # one item per concept


def test_the_cap_creates_the_oldest_first_and_reports_the_rest(gate, monkeypatch) -> None:
    monkeypatch.setattr(attention, "today", lambda: TODAY + timedelta(days=400))
    monkeypatch.setattr(M, "ATTENTION_CAP", 2)

    due = sorted((spec["origin"]["stale_on"], spec["origin"]["path"])
                 for spec in attention.plan(gate.writer, today=attention.today()))

    first = begin(gate, "WAIO-1")["attention"]

    assert (first["due"], first["created"], first["cap"], first["deferred"]) == (6, 2, 2, 4), first
    assert sorted((item["origin"]["stale_on"], item["origin"]["path"]) for item in items(gate)) == due[:2]
    end(gate, "WAIO-1")
    second = begin(gate, "WAIO-2")["attention"]  # the next run takes the next oldest
    assert (second["duplicate"], second["created"], second["deferred"]) == (2, 2, 2), second
    assert sorted((item["origin"]["stale_on"], item["origin"]["path"]) for item in items(gate)) == due[:4]


def test_a_failure_while_collecting_never_blocks_the_lease(gate, monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise OSError("disk read failed")

    monkeypatch.setattr(attention, "plan", broken)

    begun = begin(gate, "WAIO-1")

    assert begun["lease"]["run"] == "WAIO-1" and M.active_lease(gate.writer, "maintainer")["run"] == "WAIO-1"
    assert begun["attention"] == {"status": "failed", "error": "OSError: disk read failed"}


def test_the_auditor_lease_collects_nothing(gate) -> None:
    unverify(gate, AIO_AB)
    leased = gate.client.post("/maint/lease/auditor", params={"bundle": "kb-a"},
                              headers=gate.headers("auditor", "AUD-x"))

    assert "attention" not in leased.json() and items(gate) == []


# --- a notice is not evidence -------------------------------------------------------------------------


def test_a_changeset_citing_a_notice_is_refused_by_its_stored_origin(gate) -> None:
    unverify(gate, AIO_AB)
    begin(gate, "WAIO-1")
    item = M.next_item(gate.writer, principal=CURATOR, run="WAIO-1")["item"]
    request = gate.request(gate.put(AIO_AB, gate.read(AIO_AB).replace("Redacted fixture body.", "Redacted.")),
                           work_items=[item["id"]])
    request["evidence"] = {"id": "attention-fix", "item_files": [f"{item['id']}/notice.md"]}
    head = gate.head()

    for dry_run in (True, False):
        refused = gate.post(request, run="WAIO-1", dry_run=dry_run)
        assert refused.status_code == 422, refused.text
        [error] = refused.json()["errors"]
        assert error["code"] == "notice_not_evidence" and "add evidence first" in error["hint"]
    gate.assert_untouched(head)
    assert M.get_item(gate.writer, item["id"])["status"] == "in_progress"

    # Renamed, the notice is still a notice: the gate reads the origin the writer stored.
    renamed = [{"name": "S1-notes.md", "content_b64": base64.b64encode(b"# my notes\n").decode(),
                "origin": {"kind": "attention", "path": AIO_AB}}]
    queued = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": "repo"}, "topic_key": "repo:x#renamed", "priority": 999, "brief": "x", "files": renamed}]})
    other = queued.json()["items"][0]["id"]
    M.resolve(gate.writer, item["id"], {"outcome": "skipped", "reason": "insufficient_evidence"},
              principal=CURATOR, run="WAIO-1")
    assert M.next_item(gate.writer, principal=CURATOR, run="WAIO-1")["item"]["id"] == other
    request.update(work_items=[other], evidence={"id": "attention-fix", "item_files": [f"{other}/S1-notes.md"]})
    assert gate.post(request, run="WAIO-1").json()["errors"][0]["code"] == "notice_not_evidence"


def test_the_ledger_decides_which_bundle_sources_an_item_may_freeze(gate) -> None:
    rel, data = ledger_entry(gate)
    item_id = gate.item(run="WAIO-1")

    def add(name: str, body: bytes, path: str = rel):
        return gate.client.post(f"/maint/items/{item_id}/files", params={"bundle": "kb-a"},
                                headers=gate.headers(run="WAIO-1"), json={
            "name": name, "content_b64": base64.b64encode(body).decode(),
            "origin": {"kind": changeset.SOURCE_KIND, "path": path}})

    assert add("src-a", data).status_code == 200
    for name, body, path in (("src-b", data + b"forged\n", rel), ("src-c", data, "sources/not-in-the-ledger.md"),
                             ("src-d", data, "sources/inbox/x.md")):
        refused = add(name, body, path)
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "not_frozen", (name, refused.text)
    # The same label on a collected item is checked too.
    smuggled = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": "repo"}, "topic_key": "repo:x#y", "brief": "x", "files": [{
            "name": "x.md", "content_b64": base64.b64encode(b"made up\n").decode(),
            "origin": {"kind": changeset.SOURCE_KIND, "path": rel}}]}]})
    assert smuggled.status_code == 422
    assert [f["name"] for f in M.get_item(gate.writer, item_id)["files"]] == ["S1-status.md", "src-a"]


# --- the CLI ---------------------------------------------------------------------------------------------


def wiki(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args] + ["--json"])
    except SystemExit as exit_:
        code = exit_.code
    out = capsys.readouterr().out
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


@pytest.fixture
def loop(gate, tmp_path, monkeypatch):
    gate.connect()
    config = tmp_path / "maint.json"
    config.write_text(json.dumps({"audits": {"resubmit": False}}), encoding="utf-8")
    monkeypatch.setattr("aiwiki.cli.doctor.run", lambda *args, **kwargs: {"ok": True, "checks": []})
    return {"gate": gate, "st": tmp_path / "st", "config": config}


def test_begin_reports_attention_and_add_evidence_freezes_a_bundle_source(loop, capsys) -> None:
    gate, st = loop["gate"], loop["st"]
    unverify(gate, AIO_AB)
    gate.connect()  # back to the curator after the auditor's review

    code, begun = wiki(capsys, "maint", "begin", "--run", "WAIO-1", "--only", "inbox", "--state-dir", st,
                       "--config", loop["config"])

    assert code == 0, begun
    assert begun["attention"]["created"] == 1 and begun["ready"] == {"attention": 1}, begun
    assert len(json.dumps(begun).encode()) <= 2048
    code, again = wiki(capsys, "maint", "begin", "--run", "WAIO-1", "--only", "inbox", "--state-dir", st,
                       "--config", loop["config"])
    assert code == 0 and again["attention"]["created"] == 1  # a repeated begin keeps the run start's counts
    code, brief = wiki(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and brief["origin"] == "attention" and brief["files"] == [{"name": "notice.md",
                                                                                "bytes": brief["files"][0]["bytes"]}]
    item_id = brief["item"]
    cited = re.findall(r"resource: /(sources/\S+)", gate.read(AIO_AB))[0]

    code, added = wiki(capsys, "maint", "add-evidence", item_id, f"source:{cited}", "--state-dir", st)

    assert code == 0, added
    assert Path(added["path"]).read_bytes() == (gate.writer / cited).read_bytes()
    stored = next(f for f in M.get_item(gate.writer, item_id)["files"] if f["name"] == added["file"])
    assert stored["origin"] | {"sha256": None} == {"kind": "bundle-source", "path": cited, "sha256": None,
                                                    "truncated": False, "redactions": 0}
    for ref in ("source:sources/../index.md", "source:sources/inbox/x.md", "source:sources/.hashes.yaml",
                "source:sources/absent.md"):
        assert wiki(capsys, "maint", "add-evidence", item_id, ref, "--state-dir", st)[0] == 2, ref

    # The proposal cites the source, never the notice, and the gate takes it.
    ws = Path(begun["workspace"])
    (ws / AIO_AB).write_text(cite((ws / AIO_AB).read_text(encoding="utf-8"), "attention-fix",
                                  claim="The cited readout puts the lift at 3%."), encoding="utf-8")
    code, verdict = wiki(capsys, "validate", "--dir", ws, "--item", item_id)
    assert code == 0, verdict
    code, receipt = wiki(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", st)
    assert code == 0 and receipt["status"] == "done", receipt
    # One part, the bundle source: the packet keeps its bytes, never the notice's.
    assert receipt["evidence"] == {"id": "attention-fix", "parts": 1, "origin_kinds": ["bundle-source"]}, receipt
    committed = [rel for rel in receipt["changed_files"] if rel.startswith("sources/") and "attention-fix" in rel]
    assert [(gate.writer / rel).read_bytes() for rel in committed] == [(gate.writer / cited).read_bytes()], receipt
    assert M.get_item(gate.writer, item_id)["status"] == "curated"


def test_a_proposal_of_a_notice_alone_is_told_to_add_evidence(loop, capsys) -> None:
    gate, st = loop["gate"], loop["st"]
    unverify(gate, AIO_AB)
    gate.connect()
    code, begun = wiki(capsys, "maint", "begin", "--run", "WAIO-1", "--only", "inbox", "--state-dir", st,
                       "--config", loop["config"])
    assert code == 0, begun
    item_id = wiki(capsys, "maint", "next", "--state-dir", st)[1]["item"]
    ws = Path(begun["workspace"])
    (ws / AIO_AB).write_text(cite((ws / AIO_AB).read_text(encoding="utf-8"), "attention-fix",
                                  claim="The cited readout puts the lift at 3%."), encoding="utf-8")

    code, refused = wiki(capsys, "validate", "--dir", ws, "--item", item_id)

    assert code == 6 and [error["code"] for error in refused["errors"]] == ["notice_not_evidence"], refused


def test_a_source_the_workspace_lacks_is_read_from_the_writer(loop, capsys) -> None:
    gate, st = loop["gate"], loop["st"]
    gate.item(run="WAIO-0")
    end(gate, "WAIO-0")
    code, begun = wiki(capsys, "maint", "begin", "--run", "WAIO-1", "--only", "inbox", "--state-dir", st,
                       "--config", loop["config"])
    assert code == 0, begun
    item_id = wiki(capsys, "maint", "next", "--state-dir", st)[1]["item"]
    rel, data = ledger_entry(gate)
    (Path(begun["workspace"]) / ".ai-wiki" / "base" / rel).unlink()

    code, added = wiki(capsys, "maint", "add-evidence", item_id, f"source:{rel}", "--state-dir", st)

    assert code == 0 and Path(added["path"]).read_bytes() == data, added


def test_the_planner_ranks_attention_between_delta_and_doc() -> None:
    assert planner.PRIORITY["delta"] < planner.PRIORITY["attention"] < planner.PRIORITY["doc"]
    assert maint._SOURCE_REF.fullmatch("source:sources/a b.md") is None


def test_notice_bytes_depend_only_on_path_version_reason_and_review(tmp_path) -> None:
    bundle = tmp_path / "kb"
    (bundle / "metrics").mkdir(parents=True)
    concept = ("---\ntype: Metric\ntitle: X\ndescription: X.\ntags: [x]\nstatus: stable\n"
               "generated: {by: process:a, at: '2026-01-01T00:00:00Z'}\n"
               "sources:\n  - {id: s, resource: /sources/s.md.source}\n---\n# Summary\nX.\n")
    (bundle / "metrics" / "x.md").write_text(concept, encoding="utf-8")
    first = attention.plan(bundle, today=date(2026, 6, 1))
    # An audit stamp (generated, verified, status) leaves the content and the notice alone.
    (bundle / "metrics" / "x.md").write_text(concept.replace(
        "status: stable\n", "status: stable\nverified: {by: process:b, at: '2026-01-02T00:00:00Z'}\n"),
        encoding="utf-8")
    second = attention.plan(bundle, today=date(2026, 7, 1))

    assert len(first) == 1 and first == second
    assert first[0]["origin"] | {"base": None} == {"kind": "attention", "path": "metrics/x.md", "reason": "stale",
                                                   "base": None, "stale_on": "2026-04-01", "basis": "default"}
    assert attention.plan(bundle, today=date(2026, 3, 31)) == []
