"""Triage review (service/triage.py): the auditor re-checks the maintainer's skips and duplicates.

The 0.3.5 run of 2026-10-27 lost durable knowledge in 32 of 37 skipped or duplicate items, and a
stricter skill rule did not change it. A second model now judges each such decision against the
item's own frozen files: ``upheld`` keeps it closed; ``missed`` reopens it for the maintainer
with ``triage.md`` (the claims and their verbatim quotes), which is never evidence. The
maintainer may close it again only with a note saying which claim is wrong and why.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gate_fixture import METRIC, Gate, cite

from aiwiki.cli import main as cli
from aiwiki.cli import workspace
from aiwiki.maint import planner
from aiwiki.service import maint_state as M
from aiwiki.service import triage

CURATOR = "process:ai-wiki-maintainer"
AUDITOR = "process:ai-wiki-auditor"
RUN, AUD = "WAIO-1", "AUD-1"
EPOCH = "2026-01-01T00:00:00Z"  # before any test's item, so every maintainer skip counts
STATUS = b"# Funnel status\n\nOwner: web team\nDecision: keep the retry cap at 3 (member, 2026-10-20)\n" \
         b"Status: rolled back on 2026-10-21\n"
NOTE = "the status file only restates the README; nothing durable"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, AIWIKI_TRIAGE_EPOCH=EPOCH)
    yield gate
    gate.close()


def queue(gate: Gate, topic: str, files: dict[str, bytes], *, kind: str = "repo", priority: int = 40) -> str:
    response = gate.client.post("/maint/items", params={"bundle": "kb-a"}, headers=gate.headers(), json={"items": [{
        "origin": {"kind": kind}, "topic_key": topic, "priority": priority, "brief": f"changes in {topic}",
        "files": [{"name": name, "content_b64": base64.b64encode(data).decode(),
                   "origin": {"kind": "git-file", "path": f"tasks/{name}"}} for name, data in files.items()]}]})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


def lease(gate: Gate, role: str, run: str, token: str) -> None:
    response = gate.client.post(f"/maint/lease/{role}", params={"bundle": "kb-a"}, headers=gate.headers(token, run))
    assert response.status_code == 200, response.text


def release(gate: Gate, role: str, run: str, token: str) -> None:
    gate.client.delete(f"/maint/lease/{role}", params={"bundle": "kb-a"}, headers=gate.headers(token, run))


def skipped(gate: Gate, topic: str = "repo:x#tasks/funnel", files: dict[str, bytes] | None = None, *,
            outcome: str = "skipped", reason: str = "no_durable_knowledge", run: str = RUN) -> str:
    """One item the maintainer claims and closes as ``outcome`` in ``run``."""
    item_id = queue(gate, topic, files or {"S1-status.md": STATUS})
    lease(gate, "maintainer", run, "curator")
    claimed = M.next_item(gate.writer, principal=CURATOR, run=run)["item"]
    assert claimed["id"] == item_id
    M.resolve(gate.writer, item_id, {"outcome": outcome, "reason": reason, "note": NOTE}, principal=CURATOR, run=run)
    release(gate, "maintainer", run, "curator")
    return item_id


def listed(gate: Gate, token: str = "auditor") -> dict:
    response = gate.client.get("/audit/backlog/triage", params={"bundle": "kb-a"}, headers=gate.headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def missed(item_id: str, *claims: dict, note: str = "a member decision and a status change were dropped") -> dict:
    return {"item": item_id, "verdict": "missed", "note": note, "missed": list(claims) or [
        {"claim": "A member decided to keep the retry cap at 3 on 2026-10-20.",
         "evidence": {"file": "S1-status.md", "lines": [4, 5]}, "target": METRIC}]}


def upheld(item_id: str) -> dict:
    return {"item": item_id, "verdict": "upheld", "note": "the file restates the README; nothing durable"}


def send(gate: Gate, *reviews: dict, token: str = "auditor", run: str | None = AUD):
    return gate.client.post("/audit/backlog/triage", params={"bundle": "kb-a"}, headers=gate.headers(token, run),
                            json={"reviews": list(reviews)})


def codes(response) -> list[tuple[str | None, str]]:
    return [(error["item"], error["code"]) for error in response.json()["errors"]]


# --- the backlog --------------------------------------------------------------------------------


def test_the_backlog_lists_maintainer_skips_and_duplicates_since_the_epoch(gate) -> None:
    skip = skipped(gate)
    duplicate = skipped(gate, "repo:x#tasks/dup", {"S1-a.md": b"a\n", "S2-b.md": b"bb\n"}, outcome="duplicate",
                        reason=f"duplicate_of:{METRIC}", run="WAIO-2")
    # Not a maintainer's own decision: an admin's hand close, the service's close of a member
    # link nothing reads, an item still open; and a skip an admin reopened since.
    hand = queue(gate, "repo:x#hand", {"S1.md": b"x\n"})
    M.admin_resolve(gate.writer, hand, {"outcome": "skipped", "reason": "out_of_scope"}, principal="human:owner")
    reopened = skipped(gate, "repo:x#reopened", {"S1.md": b"z\n"}, run="WAIO-3")
    M.admin_retry(gate.writer, reopened, principal="human:owner", reason="look again")
    queue(gate, "repo:x#open", {"S1.md": b"y\n"})

    found = listed(gate)

    assert [row["item"] for row in found["items"]] == [skip, duplicate] and found["epoch"] == EPOCH
    assert (found["shown"], found["total"], found["truncated"]) == (2, 2, False)
    first, second = found["items"]
    assert first == {"item": skip, "topic_key": "repo:x#tasks/funnel", "origin": "repo",
                     "brief": "changes in repo:x#tasks/funnel", "status": "skipped",
                     "reason": "no_durable_knowledge", "note": NOTE, "resolved_by": CURATOR,
                     "resolved_at": first["resolved_at"], "run": RUN, "duplicate_of": None,
                     "files": [{"name": "S1-status.md", "bytes": len(STATUS),
                                "sha256": M.get_item(gate.writer, skip)["files"][0]["sha256"]}]}
    assert (second["status"], second["duplicate_of"], second["run"]) == ("duplicate", METRIC, "WAIO-2")
    assert [file["name"] for file in second["files"]] == ["S1-a.md", "S2-b.md"]
    limited = gate.client.get("/audit/backlog/triage", params={"bundle": "kb-a", "limit": 1},
                              headers=gate.headers("auditor")).json()
    assert [row["item"] for row in limited["items"]] == [skip] and limited["truncated"] is True


def test_without_an_epoch_the_feature_is_off_and_one_later_counts_only_newer_decisions(gate) -> None:
    item_id = skipped(gate)
    gate.app()
    assert listed(gate) == {"epoch": None, "items": [], "shown": 0, "total": 0, "truncated": False}
    refused = send(gate, upheld(item_id))
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "triage_off"
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers()).json()
    assert status["triage"] == {"epoch": None, "backlog": 0, "oldest_resolved_at": None,
                                "reopened": {"open": 0, "total": 0}}

    later = (datetime.now(UTC) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate.app(AIWIKI_TRIAGE_EPOCH=later)
    assert listed(gate)["total"] == 0  # decided before the operator turned it on
    with pytest.raises(RuntimeError, match="AIWIKI_TRIAGE_EPOCH must be an ISO 8601 time with a zone"):
        gate.app(AIWIKI_TRIAGE_EPOCH="2026-10-28")


def test_the_read_mirror_and_a_disabled_audit_area_refuse_triage(gate) -> None:
    item_id = skipped(gate)
    for env in ({"AIWIKI_CURATE": "off"}, {"AIWIKI_DISABLE": "ingest,audit,create,delete,changesets,workspace,"
                                                             "maint,admin"}):
        gate.app(AIWIKI_TRIAGE_EPOCH=EPOCH, **env)
        assert gate.client.get("/audit/backlog/triage", params={"bundle": "kb-a"},
                               headers=gate.headers("auditor")).status_code == 403, env
        assert send(gate, upheld(item_id)).status_code == 403, env
    assert triage.receipts(gate.writer) == []


# --- reviews ------------------------------------------------------------------------------------


def test_a_review_needs_the_runs_live_auditor_lease(gate) -> None:
    item_id = skipped(gate)
    for run in (None, AUD):  # no run at all, or a run that never took the lease
        refused = send(gate, upheld(item_id), run=run)
        assert refused.status_code == 409 and codes(refused) == [(None, "lease_required")], refused.text
    lease(gate, "auditor", "AUD-0", "auditor")
    refused = send(gate, upheld(item_id))  # another run holds it
    assert refused.status_code == 409 and refused.json()["errors"][0]["run"] == "AUD-0"
    assert send(gate, upheld(item_id), token="curator").status_code == 403  # no audit scope
    release(gate, "auditor", "AUD-0", "auditor")
    lease(gate, "auditor", AUD, "auditor")

    assert send(gate, upheld(item_id)).status_code == 200
    assert triage.receipts(gate.writer)[0]["run"] == AUD


def test_upheld_closes_the_item_for_triage_and_a_resend_returns_its_receipt(gate) -> None:
    item_id = skipped(gate)
    lease(gate, "auditor", AUD, "auditor")

    answer = send(gate, upheld(item_id)).json()

    [row] = answer["reviews"]
    assert (row["item"], row["verdict"], row["status"], row["deduplicated"]) == (item_id, "upheld", "done", False)
    assert answer["applied"] == 1 and listed(gate)["total"] == 0
    assert M.get_item(gate.writer, item_id)["status"] == "skipped"  # nothing reopened
    [receipt] = triage.receipts(gate.writer)
    assert receipt["id"] == row["id"] and (receipt["principal"], receipt["actor"]) == (AUDITOR, AUDITOR)
    assert receipt["judged"]["status"] == "skipped" and receipt["judged"]["by"] == CURATOR
    assert (gate.writer / ".okf" / "triage" / f"{row['id']}.json").is_file()

    again = send(gate, upheld(item_id)).json()  # a resend after a lost answer
    assert again["reviews"][0] | {"deduplicated": False} == row and again["reviews"][0]["deduplicated"] is True
    assert again["applied"] == 0 and len(triage.receipts(gate.writer)) == 1
    other = send(gate, {**upheld(item_id), "note": "a different note"})  # judged already: not a resend
    assert other.status_code == 409 and codes(other) == [(item_id, "not_due")]


def test_missed_reopens_the_item_with_a_triage_record_that_is_never_evidence(gate) -> None:
    item_id = skipped(gate)
    before = M.get_item(gate.writer, item_id)
    lease(gate, "auditor", AUD, "auditor")

    answer = send(gate, missed(item_id)).json()

    [row] = answer["reviews"]
    assert (row["verdict"], row["status"], row["missed"]) == ("missed", "done", 1), answer
    item = M.get_item(gate.writer, item_id)
    assert item["status"] == "ready" and item["attempts"]["started"] == 0 and item["resolution"] is None
    assert item["priority"] == planner.PRIORITY["attention"] == 45 > before["priority"]
    assert item["reopened"][-1] | {"at": None, "resolution": None} == {
        "by": AUDITOR, "at": None, "from": "skipped", "resolution": None, "reason": "triage_missed",
        "review": row["id"]}
    record = item["files"][-1]
    assert record["name"] == "triage.md" and record["origin"] == {"kind": "triage", "review": row["id"],
                                                                  "auditor": AUDITOR}
    text = M.read_file(gate.writer, item_id, "triage.md").decode()
    assert f"- review: {row['id']} at " in text and f"- auditor: {AUDITOR}" in text
    assert "- claim: A member decided to keep the retry cap at 3 on 2026-10-20." in text
    assert f"- target: `{METRIC}`" in text and "- evidence: `S1-status.md` lines 4-5, verbatim:" in text
    # Verbatim from the frozen file; facts only, never guidance.
    assert "> Decision: keep the retry cap at 3 (member, 2026-10-20)\n> Status: rolled back on 2026-10-21\n" in text
    assert "skip" not in text.lower().replace("judged: skipped", "")
    assert item["brief"].startswith(f"Triage {row['id']}: this skipped missed 1 claim(s), quoted in triage.md")
    assert METRIC in item["brief"] and item["brief"].endswith(f"Collected: {before['brief']}")
    assert listed(gate)["total"] == 0  # judged: a reopened item is no longer a skip to review

    # Reopened misses are served before plain deltas.
    plain = queue(gate, "repo:x#plain", {"S1.md": b"plain\n"}, priority=40)
    lease(gate, "maintainer", "WAIO-2", "curator")
    assert M.next_item(gate.writer, principal=CURATOR, run="WAIO-2")["item"]["id"] == item_id

    # The gate drops triage.md from a packet and refuses a packet of nothing else.
    request = gate.request(gate.put(METRIC, cite(gate.read(METRIC), "triage-fix",
                                                 claim="A member kept the retry cap at 3.")), work_items=[item_id])
    request["evidence"] = {"id": "triage-fix", "item_files": [f"{item_id}/triage.md"]}
    head = gate.head()
    for dry_run in (True, False):
        refused = gate.post(request, run="WAIO-2", dry_run=dry_run)
        assert refused.status_code == 422 and refused.json()["errors"][0]["code"] == "notice_not_evidence", \
            refused.text
    gate.assert_untouched(head)
    request["evidence"]["item_files"] = [f"{item_id}/S1-status.md", f"{item_id}/triage.md"]
    committed = gate.post(request, run="WAIO-2")
    assert committed.status_code == 201, committed.text
    packet = next((gate.writer / "sources").glob("triage-fix-*"))
    assert packet.read_bytes() == STATUS  # the item's own file alone: one part, its bytes verbatim
    assert M.get_item(gate.writer, item_id)["status"] == "curated" and plain


def test_a_missed_claim_must_quote_a_frozen_text_file_of_the_item(gate) -> None:
    item_id = skipped(gate, files={"S1-status.md": STATUS, "S2-chart.png": b"\x89PNG\r\n\x1a\n\xff\xfe"})
    other = skipped(gate, "repo:x#other", {"S1-other.md": b"other\n"}, run="WAIO-2")
    lease(gate, "auditor", AUD, "auditor")

    def claim(**change) -> dict:
        base = {"claim": "A member decided to keep the retry cap at 3.",
                "evidence": {"file": "S1-status.md", "lines": [4, 4]}, "target": METRIC}
        return {**base, **change}

    cases = {
        "evidence": [claim(evidence={"file": "S1-other.md", "lines": [1, 1]}),  # another item's file
                     claim(evidence={"file": "S1-status.md", "lines": [6, 7]}),  # past the end
                     claim(evidence={"file": "S2-chart.png", "lines": [1, 1]})],  # no line of an image
        "input": [claim(evidence={"file": "S1-status.md", "lines": [0, 1]}), claim(evidence={"file": "S1-status.md",
                                                                                                "lines": [5, 4]}),
                  claim(evidence={"file": "S1-status.md", "lines": [1, 51]}), claim(claim="x" * 501),
                  claim(claim="two\nlines"), claim(target="sources/x.md"), claim(target="../x.md"),
                  claim(target="x.md"), claim(target="metrics/x.txt"), {"claim": "c", "target": METRIC}],
        "secret_detected": [claim(claim="the key is AKIA" + "Q" * 16)],
    }
    for code, claims in cases.items():
        for bad in claims:
            refused = send(gate, missed(item_id, bad))
            assert codes(refused) == [(item_id, code)], (bad, refused.text)
            assert refused.status_code == triage.STATUS[code]
    assert codes(send(gate, missed(item_id, *[claim()] * 11))) == [(item_id, "input")]  # 1 to 10 claims
    assert codes(send(gate, {**missed(item_id), "missed": []})) == [(item_id, "input")]
    assert codes(send(gate, {**upheld(item_id), "missed": [claim()]})) == [(item_id, "input")]
    assert codes(send(gate, {**upheld(item_id), "note": "token aiw_c_" + "x" * 24})) == [(item_id, "secret_detected")]
    assert codes(send(gate, {**upheld(item_id), "extra": 1})) == [(item_id, "input")]
    # All or nothing: one bad review refuses the other too, and nothing is written.
    refused = send(gate, upheld(other), missed(item_id, claim(evidence={"file": "S1-other.md", "lines": [1, 1]})))
    assert codes(refused) == [(item_id, "evidence")] and triage.receipts(gate.writer) == []
    assert codes(send(gate, upheld(other), upheld(other))) == [(other, "input")]  # one review per item
    too_many = send(gate, *[upheld(other)] * 11)
    assert too_many.status_code == 413 and codes(too_many) == [(None, "too_large")]
    assert codes(send(gate, upheld("it_000000000000"))) == [("it_000000000000", "not_due")]
    # An item still open, or curated, is not in the backlog.
    curated_or_open = queue(gate, "repo:x#open", {"S1.md": b"open\n"})
    assert codes(send(gate, upheld(curated_or_open))) == [(curated_or_open, "not_due")]

    landed = send(gate, missed(item_id, claim(), claim(evidence={"file": "S1-status.md", "lines": [1, 3]},
                                                       target="decisions/retry-cap.md")))
    assert landed.status_code == 200, landed.text
    text = M.read_file(gate.writer, item_id, "triage.md").decode()
    assert "## Claim 2" in text and "- target: `decisions/retry-cap.md`" in text
    assert "> # Funnel status\n>\n> Owner: web team\n" in text  # a blank line quoted as such


def test_the_auditor_never_reviews_a_decision_its_own_principal_made(gate) -> None:
    """A4's rule for triage: the auditor never resolves items, but the writer enforces it anyway.
    The owner's token holds both scopes, so its own skip is the case that can happen."""
    item_id = queue(gate, "repo:x#owner", {"S1.md": b"owner\n"})
    lease(gate, "maintainer", RUN, "owner")
    M.next_item(gate.writer, principal="human:owner", run=RUN)
    M.resolve(gate.writer, item_id, {"outcome": "skipped", "reason": "out_of_scope"}, principal="human:owner",
              run=RUN)
    release(gate, "maintainer", RUN, "owner")
    assert listed(gate)["items"][0]["resolved_by"] == "human:owner"
    lease(gate, "auditor", AUD, "owner")

    refused = send(gate, upheld(item_id), token="owner")

    assert refused.status_code == 409 and codes(refused) == [(item_id, "self_review")]
    release(gate, "auditor", AUD, "owner")
    lease(gate, "auditor", AUD, "auditor")
    assert send(gate, upheld(item_id)).status_code == 200  # another principal may


def test_triage_reviews_share_the_reviews_per_day_quota(gate) -> None:
    gate.app(AIWIKI_TRIAGE_EPOCH=EPOCH, AIWIKI_REVIEWS_PER_DAY="2")
    items = [skipped(gate, f"repo:x#t{n}", {"S1.md": f"item {n}\n".encode()}, run=f"WAIO-{n}") for n in range(4)]
    lease(gate, "auditor", AUD, "auditor")
    assert send(gate, upheld(items[0])).status_code == 200

    refused = send(gate, upheld(items[1]), upheld(items[2]))

    assert refused.status_code == 429 and refused.json()["errors"][0]["limit"] == "reviews_per_day"
    assert int(refused.headers["Retry-After"]) > 0 and len(triage.receipts(gate.writer)) == 1
    assert send(gate, upheld(items[1])).status_code == 200
    assert send(gate, upheld(items[0])).status_code == 200  # a resend is never charged again
    assert send(gate, upheld(items[3])).status_code == 429


def test_a_crash_between_the_receipt_and_its_effect_is_finished_by_the_next_request(gate, monkeypatch) -> None:
    item_id = skipped(gate)
    lease(gate, "auditor", AUD, "auditor")
    reopen = M.triage_reopen

    def crash(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(M, "triage_reopen", crash)
    with pytest.raises(OSError):
        send(gate, missed(item_id))
    [receipt] = triage.receipts(gate.writer)
    assert receipt["status"] == "applying" and M.get_item(gate.writer, item_id)["status"] == "skipped"
    assert listed(gate)["total"] == 1  # applying judges nothing yet
    monkeypatch.setattr(M, "triage_reopen", reopen)

    answer = send(gate, missed(item_id)).json()

    assert answer["reviews"][0]["status"] == "done" and answer["reviews"][0]["deduplicated"] is True
    assert M.get_item(gate.writer, item_id)["status"] == "ready"
    assert [entry["reason"] for entry in M.get_item(gate.writer, item_id)["reopened"]] == ["triage_missed"]
    # Reopening twice under one receipt changes nothing.
    again = M.triage_reopen(gate.writer, item_id, by=AUDITOR, review=receipt["id"],
                            file=({"name": "x.md", "sha256": "0" * 64, "bytes": 1, "origin": {}}, b"x"),
                            lead="x", priority=45)
    assert [f["name"] for f in again["files"]] == ["S1-status.md", "triage.md"]


def test_a_missed_review_whose_item_an_admin_reopened_meanwhile_judges_nothing(gate, monkeypatch) -> None:
    """The receipt is written before its effect; if the item stopped being a skip in between, the
    review is void (it judged nothing) and never blocks a later review of a later decision."""
    item_id = skipped(gate)
    lease(gate, "auditor", AUD, "auditor")
    reopen = M.triage_reopen

    def admin_first(bundle, item, **kwargs):
        M.admin_retry(bundle, item, principal="human:owner", reason="look again")
        return reopen(bundle, item, **kwargs)

    monkeypatch.setattr(M, "triage_reopen", admin_first)
    [row] = send(gate, missed(item_id)).json()["reviews"]
    monkeypatch.setattr(M, "triage_reopen", reopen)

    assert row["status"] == "void" and row["detail"].startswith("not_reopenable"), row
    item = M.get_item(gate.writer, item_id)
    assert item["status"] == "ready" and [f["name"] for f in item["files"]] == ["S1-status.md"]
    assert [entry["reason"] for entry in item["reopened"]] == ["look again"]
    assert send(gate, missed(item_id)).json()["reviews"][0]["deduplicated"] is True  # a resend: the same receipt
    # The maintainer closes it again: a new decision, due again, reviewed anew.
    lease(gate, "maintainer", "WAIO-2", "curator")
    M.next_item(gate.writer, principal=CURATOR, run="WAIO-2")
    M.resolve(gate.writer, item_id, {"outcome": "skipped", "reason": "out_of_scope"}, principal=CURATOR, run="WAIO-2")
    assert [row["item"] for row in listed(gate)["items"]] == [item_id]
    [again] = send(gate, missed(item_id)).json()["reviews"]
    assert again["status"] == "done" and again["id"] != row["id"]
    # A receipt that does not name its own file is never read.
    forged = gate.writer / ".okf" / "triage" / "tr_000000000000.json"
    forged.write_text(json.dumps({"id": "tr_ffffffffffff", "item": item_id, "verdict": "upheld", "status": "done",
                                  "judged": {}, "missed": []}), encoding="utf-8")
    assert len(triage.receipts(gate.writer)) == 2


# --- the maintainer side ------------------------------------------------------------------------


def test_the_maintainer_closes_a_triage_reopened_item_again_only_with_grounds(gate) -> None:
    item_id = skipped(gate)
    lease(gate, "auditor", AUD, "auditor")
    assert send(gate, missed(item_id)).status_code == 200
    lease(gate, "maintainer", "WAIO-2", "curator")
    assert M.next_item(gate.writer, principal=CURATOR, run="WAIO-2")["item"]["id"] == item_id

    for body in ({"outcome": "skipped", "reason": "no_durable_knowledge"},
                 {"outcome": "skipped", "reason": "insufficient_evidence", "note": "still nothing"},
                 {"outcome": "duplicate", "reason": f"duplicate_of:{METRIC}", "note": " " * 50}):
        refused = gate.client.post(f"/maint/items/{item_id}/resolve", params={"bundle": "kb-a"},
                                   headers=gate.headers(run="WAIO-2"), json=body)
        assert refused.status_code == 400 and refused.json()["detail"]["code"] == "triage_note_required", body
    assert M.get_item(gate.writer, item_id)["status"] == "in_progress"
    # Parking or splitting it needs no such note; a skip that explains itself is accepted.
    grounds = "claim 1 is wrong: line 4 is a proposal, the decision itself is in WAIO-9, not in this file"
    closed = gate.client.post(f"/maint/items/{item_id}/resolve", params={"bundle": "kb-a"},
                              headers=gate.headers(run="WAIO-2"),
                              json={"outcome": "skipped", "reason": "no_durable_knowledge", "note": grounds})
    assert closed.status_code == 200 and closed.json()["resolution"]["note"] == grounds
    # Judged once: the second close is not reviewed again, so the two never loop.
    assert listed(gate)["total"] == 0
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers()).json()["triage"]
    assert status == {"epoch": EPOCH, "backlog": 0, "oldest_resolved_at": None, "reopened": {"open": 0, "total": 1}}


def test_maint_status_counts_the_triage_backlog_and_reopened_items(gate) -> None:
    first = skipped(gate)
    second = skipped(gate, "repo:x#second", {"S1.md": b"second\n"}, run="WAIO-2")
    resolved = M.get_item(gate.writer, first)["resolution"]["at"]

    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers()).json()["triage"]

    assert status == {"epoch": EPOCH, "backlog": 2, "oldest_resolved_at": resolved,
                      "reopened": {"open": 0, "total": 0}}
    lease(gate, "auditor", AUD, "auditor")
    assert send(gate, missed(first), upheld(second)).status_code == 200
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers()).json()["triage"]
    assert status["backlog"] == 0 and status["reopened"] == {"open": 1, "total": 1}


def test_a_triage_lead_survives_a_merge_of_newer_evidence(gate) -> None:
    item_id = skipped(gate)
    lease(gate, "auditor", AUD, "auditor")
    assert send(gate, missed(item_id)).status_code == 200
    lead = M.get_item(gate.writer, item_id)["brief"].split(" Collected: ")[0]

    merged = queue(gate, "repo:x#tasks/funnel", {"S1-status.md": STATUS + b"Update: done\n"})

    item = M.get_item(gate.writer, item_id)
    assert merged == item_id and item["brief"] == f"{lead} Collected: changes in repo:x#tasks/funnel"
    assert [f["name"] for f in item["files"]] == ["S1-status.md", "triage.md"]


# --- the auditor's CLI --------------------------------------------------------------------------


def wiki(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in (*args, "--json")])
    except SystemExit as exit_:
        code = exit_.code
    out = capsys.readouterr().out
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


def test_the_review_triage_verbs_end_to_end(gate, capsys, monkeypatch, tmp_path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(workspace, "RETRY_DELAYS_S", (0, 0, 0))
    gate.app(AIWIKI_TRIAGE_EPOCH=EPOCH, AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH="2026-09-20T00:00:00Z")
    first = skipped(gate)
    second = skipped(gate, "repo:x#dup", {"S1-dup.md": b"same as the funnel metric\n"}, outcome="duplicate",
                     reason=f"duplicate_of:{METRIC}", run="WAIO-2")
    third = skipped(gate, "repo:x#third", {"S1-third.md": b"third\n"}, run="WAIO-3")
    gate.connect("auditor")
    state = tmp_path / "auditor-state"

    code, begun = wiki(capsys, "review", "begin", "--run", AUD, "--triage-max", 2, "--state-dir", state)
    assert code == 0 and begun["triage"] == {"backlog": 3, "epoch": EPOCH, "max": 2, "taken": 0}, begun

    code, taken = wiki(capsys, "review", "triage", "next", "--state-dir", state)
    assert code == 0 and (taken["item"], taken["status"], taken["reason"]) == (first, "skipped",
                                                                                "no_durable_knowledge"), taken
    [file] = taken["files"]
    assert Path(file["path"]).read_bytes() == STATUS and taken["duplicate_of"] is None and taken["note"] == NOTE
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([{"claim": "A member decided to keep the retry cap at 3.",
                                   "evidence": {"file": "S1-status.md", "lines": [9, 9]}, "target": METRIC}]))
    code, refused = wiki(capsys, "review", "triage", "verdict", first, "missed", "--note", "dropped a decision",
                         "--missed-file", claims, "--state-dir", state)
    assert code == 2 and "evidence" in refused["error"], refused  # checked here first: the file has 5 lines
    assert wiki(capsys, "review", "triage", "verdict", first, "missed", "--note", "n", "--state-dir", state)[0] == 2
    claims.write_text(json.dumps([{"claim": "A member decided to keep the retry cap at 3.",
                                   "evidence": {"file": "S1-status.md", "lines": [4, 4]}, "target": METRIC}]))
    code, recorded = wiki(capsys, "review", "triage", "verdict", first, "missed", "--note", "dropped a decision",
                          "--missed-file", claims, "--state-dir", state)
    assert code == 0 and recorded["pending"] == 1

    code, taken = wiki(capsys, "review", "triage", "next", "--state-dir", state)
    assert code == 0 and taken["item"] == second
    assert taken["duplicate_of"] == {"path": METRIC, "concept": str(Path(begun["workspace"]) / METRIC), "exists": True}
    assert wiki(capsys, "review", "triage", "verdict", second, "upheld", "--note", "the metric states it all",
                "--state-dir", state)[0] == 0
    assert wiki(capsys, "review", "triage", "next", "--state-dir", state)[0] == 11  # --triage-max 2
    assert wiki(capsys, "review", "triage", "verdict", third, "upheld", "--note", "x", "--state-dir", state)[0] == 2

    code, sent = wiki(capsys, "review", "triage", "submit", "--state-dir", state)

    assert code == 0 and sent["pending"] == 0, sent
    assert sorted((row["item"], row["verdict"], row["status"]) for row in sent["reviews"]) == sorted(
        [(first, "missed", "done"), (second, "upheld", "done")])
    assert M.get_item(gate.writer, first)["status"] == "ready"
    code, report = wiki(capsys, "review", "end", "--run", AUD, "--state-dir", state)
    assert code == 0 and report["triage"] == {"reviewed": 2, "upheld": 1, "missed": 1, "void": 0, "dropped": [],
                                              "unsubmitted": [], "backlog_remaining": 1}, report
    assert report["lease"]["released"] is True

    # The maintainer's next run: the reopened item first, triage.md beside its evidence.
    gate.connect("curator")
    st, config = tmp_path / "st", tmp_path / "maint.json"
    config.write_text(json.dumps({"audits": {"resubmit": False}}), encoding="utf-8")
    (tools / "multica").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "multica").chmod(0o755)
    code, begun = wiki(capsys, "maint", "begin", "--run", "WAIO-9", "--only", "inbox", "--config", config,
                       "--state-dir", st)
    assert code == 0, begun
    code, brief = wiki(capsys, "maint", "next", "--state-dir", st)
    assert code == 0 and brief["item"] == first and brief["brief"].startswith("Triage tr_"), brief
    assert brief["priority"] == 45 and [file["name"] for file in brief["files"]] == ["S1-status.md", "triage.md"]
    code, refused = wiki(capsys, "maint", "skip", first, "--reason", "no_durable_knowledge", "--state-dir", st)
    assert code == 2 and refused["detail"]["code"] == "triage_note_required", refused
    ws = Path(begun["workspace"])
    (ws / METRIC).write_text(cite((ws / METRIC).read_text(encoding="utf-8"), "retry-cap-2026-10-20",
                                  claim="A member decided to keep the retry cap at 3."), encoding="utf-8")
    code, receipt = wiki(capsys, "propose", "--dir", ws, "--item", first, "--state-dir", st)
    assert code == 0 and receipt["status"] == "done", receipt
    assert M.get_item(gate.writer, first)["status"] == "curated"
    packet = next((gate.writer / "sources").glob("retry-cap-2026-10-20-*"))
    assert packet.read_bytes() == STATUS  # the item's own file alone: triage.md never enters a packet


def test_review_triage_submit_drops_what_the_writer_refuses_and_sends_the_rest(gate, capsys, monkeypatch,
                                                                               tmp_path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    gate.app(AIWIKI_TRIAGE_EPOCH=EPOCH, AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH="2026-09-20T00:00:00Z")
    first = skipped(gate)
    second = skipped(gate, "repo:x#second", {"S1.md": b"second\n"}, run="WAIO-2")
    gate.connect("auditor")
    state = tmp_path / "auditor-state"
    assert wiki(capsys, "review", "begin", "--run", AUD, "--state-dir", state)[0] == 0
    for item_id in (first, second):
        assert wiki(capsys, "review", "triage", "next", "--state-dir", state)[1]["item"] == item_id
        assert wiki(capsys, "review", "triage", "verdict", item_id, "upheld", "--note", "nothing durable",
                    "--state-dir", state)[0] == 0
    M.admin_retry(gate.writer, second, principal="human:owner", reason="look again")  # no longer due

    code, sent = wiki(capsys, "review", "triage", "submit", "--state-dir", state)

    assert code == 6 and sent["dropped"] == [{"item": second, "code": "not_due"}] and sent["pending"] == 0, sent
    assert [row["item"] for row in sent["reviews"]] == [first]
    assert wiki(capsys, "review", "end", "--run", AUD, "--state-dir", state)[1]["triage"]["dropped"] == sent["dropped"]
