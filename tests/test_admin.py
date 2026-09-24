"""Incident response on the writer (design §8.5, acceptance §13 W11): GET /admin/changesets and
POST /admin/revert, plus the earlier published revisions GET /workspace serves ``admin compare``.

A revert is one deterministic commit through the writer: newest first, each file a changeset
wrote returns to its bytes before that commit while it still holds the commit's bytes, the
service-owned files are rebuilt, and the first conflict stops it.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from gate_fixture import METRIC, OPERATOR, Gate, cited, clone, concept, git, wait_for

from aiwiki.engine.lint import lint
from aiwiki.runtime import curate
from aiwiki.runtime import revert as revert_runtime
from aiwiki.service import ingest as I
from aiwiki.service import maint_state as M
from aiwiki.service import worker

PROBE = "metrics/probe.md"
OWNED = "metrics/owner-note.md"
VISITORS = "metrics/web-landing-new-visitor-distribution-2026-09.md"
WATCHDOG = Path(__file__).resolve().parents[1] / "scripts" / "maintenance_watchdog.py"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    yield gate
    gate.close()


def revert(gate: Gate, body: dict, *, token: str = "owner", run: str | None = None):
    return gate.client.post("/admin/revert", params={"bundle": "kb-a"}, json=body, headers=gate.headers(token, run))


def listing(gate: Gate, **params):
    return gate.client.get("/admin/changesets", params={"bundle": "kb-a", **params}, headers=gate.headers("owner"))


def commit(gate: Gate, *files: dict) -> dict:
    response = gate.post(gate.request(*files))
    assert response.status_code == 201, response.text
    return response.json()


def created_at(gate: Gate, job_id: str, when: str) -> None:
    job = gate.job(job_id)
    job["created"] = when
    I.save_job(gate.writer, job)


def external_push(gate: Gate, rel: str, old: str, new: str) -> str:
    """Someone pushes an edit to ``rel`` past the writer; the next transaction syncs onto it."""
    other = clone(gate.remote, gate.tmp / f"other-{len(list(gate.tmp.glob('other-*')))}")
    path = other / rel
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    git(other, "commit", "-qam", f"hand edit: {rel}")
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def outside_ledger(gate: Gate, old: str, new: str) -> str:
    return git(gate.writer, "diff", "--name-only", old, new, "--", ".", ":!log.md", ":!viz.html")


# --- revert -------------------------------------------------------------------------------------


def test_a_revert_is_a_deterministic_commit_back_to_the_tree_before_the_changeset(gate) -> None:
    head = gate.head()
    changeset = commit(gate, gate.put(PROBE, concept("Probe")), gate.put(METRIC, cited(gate.read(METRIC))))
    assert outside_ledger(gate, head, changeset["commit"])  # a new concept, a packet, indexes

    response = revert(gate, {"changeset": changeset["id"], "reason": "curator token leaked"}, run="INC-7")

    assert response.status_code == 201, response.text
    job = response.json()
    assert (job["kind"], job["status"], job["reverted"], job["deduplicated"]) == (
        "revert", "done", [changeset["id"]], False)
    assert (job["principal"], job["actor"], job["run"]) == ("human:owner", "human:owner", "INC-7")
    assert job["commit"] == gate.remote_head() == gate.head() and job["git"]["pushed"] is True
    assert job["concept_files"] == [METRIC, PROBE] and job["kept_sources"] == []
    # Every file is back to its bytes before the changeset: the concepts, the packet, the
    # indexes and the source ledger. Only the append-only log and the visualization move on.
    assert outside_ledger(gate, head, job["commit"]) == ""
    assert not (gate.writer / PROBE).exists() and not (gate.writer / changeset["source_snapshot"]).exists()
    log = gate.read("log.md")
    assert log.index("**Revert**: Reverted changeset " + changeset["id"]) < log.index(
        f"Changeset {changeset['id']} curated")
    assert git(gate.writer, "log", "-1", "--format=%B").splitlines() == [
        f"revert: changeset {changeset['id']} (curator token leaked)", "",
        f"Revert: {job['id']}", f"Reverts-Changeset: {changeset['id']} {changeset['commit']}",
        "Principal: human:owner", "Run: INC-7"]
    assert git(gate.writer, "status", "--porcelain") == ""
    rows = listing(gate).json()["changesets"]
    assert [(row["id"], row["reverted_by"]) for row in rows] == [(changeset["id"], job["id"])]
    # Nothing of the changeset is left to audit, so it never returns as pending audit work.
    assert changeset["id"] not in {row["id"] for row in I.pending_audits(gate.writer, older_than_hours=0)["jobs"]}


def test_a_revert_by_principal_goes_newest_first_and_stops_at_the_first_conflict(gate) -> None:
    head = gate.head()
    older = commit(gate)  # METRIC
    hand = external_push(gate, METRIC, "The funnel moved.", "The funnel moved again.")
    newer = commit(gate, gate.put(VISITORS, cited(gate.read(VISITORS))))
    owned = gate.post(gate.request(gate.put(OWNED, concept("Owner note"))), token="owner")
    assert owned.status_code == 201
    for job_id in (older["id"], newer["id"]):
        created_at(gate, job_id, "2026-09-24T01:00:00Z")
    before = gate.read(METRIC)

    response = revert(gate, {"principal": OPERATOR, "since": "2026-09-24"})

    assert response.status_code == 201, response.text
    job = response.json()
    assert {entry["id"] for entry in job["changesets"]} == {older["id"], newer["id"]}
    assert job["reverted"] == [newer["id"]]
    assert job["stopped"] == {"changeset": older["id"], "commit": older["commit"], "conflicts": [{
        "code": "conflict", "path": METRIC, "changed_by": hand,
        "message": "the file changed after the changeset",
        "hint": "workspace pull, then re-apply the change to the current version"}]}
    assert job["pending"] == []
    assert git(gate.writer, "show", f"{head}:{VISITORS}") == gate.read(VISITORS).rstrip("\n")
    assert gate.read(METRIC) == before and "The funnel moved again." in before
    assert (gate.writer / OWNED).is_file()  # the owner's changeset is not the curator's
    assert git(gate.writer, "log", "-1", "--format=%B").count("Reverts-Changeset:") == 1

    # The older one alone: the newest changeset of the request conflicts, so nothing is committed.
    tip = gate.head()
    conflict = revert(gate, {"changeset": older["id"]})
    assert conflict.status_code == 409
    rejected = conflict.json()
    assert (rejected["status"], rejected["reverted"], rejected["failure"]["class"]) == ("rejected", [], "conflict")
    assert [(error["code"], error["path"], error["changed_by"]) for error in rejected["errors"]] == [
        ("conflict", METRIC, hand)]
    assert gate.remote_head() == gate.head() == tip and git(gate.writer, "status", "--porcelain") == ""


def test_an_audit_stamp_on_the_changeset_does_not_stop_its_revert(gate) -> None:
    before = gate.read(METRIC)
    changeset = commit(gate)
    stamp = "verified:\n- {by: process:ai-wiki-adversarial-audit, at: '2099-01-01T00:00:00Z'}\n"
    audit = external_push(gate, METRIC, "verified:\n", stamp)  # the Codex audit of the changeset
    corrected = commit(gate, gate.put(VISITORS, cited(gate.read(VISITORS))))
    external_push(gate, VISITORS, "verified:\n", stamp)
    correction = external_push(gate, VISITORS, "The funnel moved.", "The funnel moved, corrected.")

    response = revert(gate, {"changeset": changeset["id"]})

    assert response.status_code == 201, response.text
    assert gate.read(METRIC) == before  # the stamp went with the content it verified
    assert git(gate.writer, "log", "-1", "--format=%H", "--", METRIC) == response.json()["commit"] != audit
    # A correction is content: the revert of that changeset stops at it, not at the stamp before it.
    stopped = revert(gate, {"changeset": corrected["id"]})
    assert stopped.status_code == 409
    assert [(error["path"], error["changed_by"]) for error in stopped.json()["errors"]] == [(VISITORS, correction)]


def test_a_revert_is_answered_once_and_never_reverts_twice(gate) -> None:
    changeset = commit(gate)
    noop = gate.post(gate.request(gate.put(METRIC, gate.read(METRIC)))).json()
    first = revert(gate, {"changeset": changeset["id"]}).json()

    resend = revert(gate, {"changeset": changeset["id"]})
    again = revert(gate, {"principal": OPERATOR, "since": "2026-01-01T00:00:00+08:00"})

    assert resend.status_code == 200 and resend.json()["id"] == first["id"] and resend.json()["deduplicated"] is True
    assert again.status_code == 200
    assert again.json() == {"status": "noop", "changesets": [], "deduplicated": False,
                            "reverted_by": {changeset["id"]: first["id"]}}
    nothing = revert(gate, {"changeset": noop["id"]})
    assert nothing.status_code == 409 and "committed nothing" in nothing.json()["detail"]
    assert revert(gate, {"changeset": "0" * 12}).status_code == 404
    for body in ({}, {"changeset": changeset["id"], "principal": OPERATOR, "since": "2026-09-24"},
                 {"principal": OPERATOR}, {"changeset": "../jobs/x"}, {"principal": OPERATOR, "since": "yesterday"},
                 {"changeset": changeset["id"], "reason": "leaked token aiw_c_" + "A" * 30}):  # every reader sees it
        assert revert(gate, body).status_code == 400, body
    assert revert(gate, {"changeset": changeset["id"]}, run="bad run").status_code == 400
    assert len([stem for stem in gate.jobs() if gate.job(stem)["kind"] == "revert"]) == 1


def test_a_resend_while_the_revert_is_queued_answers_with_that_revert(gate) -> None:
    changeset = commit(gate)
    gate.app(AIWIKI_CHANGESET_WAIT_S="0")
    with worker.serialized_mutation():  # the worker cannot start the revert yet
        first = revert(gate, {"principal": OPERATOR, "since": "2020-01-01"})
        resend = revert(gate, {"principal": OPERATOR, "since": "2020-01-01"})
        assert first.status_code == resend.status_code == 202
        assert resend.json()["id"] == first.json()["id"] and resend.json()["deduplicated"] is True
        assert resend.headers["Location"] == first.headers["Location"]
    wait_for(lambda: gate.job(first.json()["id"])["status"] == "done")
    later = revert(gate, {"principal": OPERATOR, "since": "2020-01-01"})
    assert later.json() == {"status": "noop", "changesets": [], "deduplicated": False,
                            "reverted_by": {changeset["id"]: first.json()["id"]}}


def test_a_reverted_changeset_proposed_again_is_new_work(gate) -> None:
    changeset = commit(gate)
    assert revert(gate, {"changeset": changeset["id"]}).status_code == 201
    assert "The funnel moved." not in gate.read(METRIC)

    again = gate.post(gate.request())  # the same content and packet, on the new base

    assert again.status_code == 201, again.text
    assert again.json()["id"] != changeset["id"] and again.json()["deduplicated"] is False
    assert again.json()["commit"] == gate.remote_head() and "The funnel moved." in gate.read(METRIC)



def test_the_work_item_of_a_reverted_changeset_can_be_reopened(gate) -> None:
    # §8.5 reverts a principal's changesets; the good evidence in their items is curated again.
    item_id = gate.item(run="WAIO-1")
    changeset = gate.post(gate.item_request(item_id), run="WAIO-1").json()
    assert M.get_item(gate.writer, item_id)["status"] == "curated"

    def retry():
        return gate.client.post(f"/admin/items/{item_id}/retry", params={"bundle": "kb-a"},
                                headers=gate.headers("owner"), json={"reason": "the revert took good work too"})

    live = retry()  # its content is still in the bundle
    assert live.status_code == 409 and live.json()["detail"]["code"] == "not_reopenable"
    assert revert(gate, {"changeset": changeset["id"]}).status_code == 201

    reopened = retry()

    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "ready" and reopened.json()["reopened"][-1]["from"] == "curated"
    assert gate.client.post("/maint/items/next", params={"bundle": "kb-a"},
                            headers=gate.headers(run="WAIO-1")).json()["item"]["id"] == item_id

def test_a_revert_that_would_break_a_later_link_changes_nothing(gate) -> None:
    created = commit(gate, gate.put(PROBE, concept("Probe")))
    linking = cited(gate.read(VISITORS)).replace("# Summary\n\n", "# Summary\n\nSee [Probe](probe.md).\n\n", 1)
    commit(gate, gate.put(VISITORS, linking))
    head = gate.head()

    response = revert(gate, {"changeset": created["id"]})

    assert response.status_code == 422, response.text
    job = response.json()
    assert (job["status"], job["reverted"], job["phase"]) == ("rejected", [], "rolled_back")
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("broken_link", VISITORS)]
    gate.assert_untouched(head)
    assert (gate.writer / PROBE).is_file()


def test_a_problem_only_an_older_changeset_leaves_stops_the_revert_there(gate) -> None:
    older = commit(gate, gate.put(PROBE, concept("Probe")))
    # The owner links the curator's new concept; the curator's newer changeset is unrelated.
    linking = cited(gate.read(VISITORS)).replace("# Summary\n\n", "# Summary\n\nSee [Probe](probe.md).\n\n", 1)
    assert gate.post(gate.request(gate.put(VISITORS, linking)), token="owner").status_code == 201
    newer = commit(gate, gate.put(METRIC, cited(gate.read(METRIC), "Bad claim.")))

    response = revert(gate, {"principal": OPERATOR, "since": "2020-01-01"})

    assert response.status_code == 201, response.text
    job = response.json()
    assert (job["reverted"], job["pending"]) == ([newer["id"]], [])
    assert job["stopped"]["changeset"] == older["id"] and job["stopped"]["commit"] == older["commit"]
    assert [(error["code"], error["path"]) for error in job["stopped"]["errors"]] == [("broken_link", VISITORS)]
    assert "Bad claim." not in gate.read(METRIC) and (gate.writer / PROBE).is_file()
    assert job["commit"] == gate.remote_head() and git(gate.writer, "status", "--porcelain") == ""
    assert git(gate.writer, "log", "-1", "--format=%B").count("Reverts-Changeset:") == 1


def test_a_restored_file_may_not_link_a_concept_the_revert_removes(gate) -> None:
    older = commit(gate, gate.put(PROBE, concept("Probe")))
    external_push(gate, VISITORS, "# Summary\n\n", "# Summary\n\nSee [Probe](probe.md).\n\n")
    git(gate.writer, "pull", "-q", "--rebase")
    newer = commit(gate, gate.put(VISITORS, cited(gate.read(VISITORS))))  # keeps the hand-written link

    response = revert(gate, {"principal": OPERATOR, "since": "2020-01-01"})

    assert response.status_code == 201, response.text
    job = response.json()
    # Undoing the older one would bring the link back to VISITORS with nothing behind it.
    assert (job["reverted"], job["stopped"]["changeset"]) == ([newer["id"]], older["id"])
    assert [(error["code"], error["path"]) for error in job["stopped"]["errors"]] == [("broken_link", VISITORS)]
    assert "See [Probe](probe.md)." in gate.read(VISITORS) and (gate.writer / PROBE).is_file()
    assert [finding for finding in lint(gate.writer)[0] if finding["severity"] == "high"] == []


def test_a_revert_refuses_pre_existing_source_drift(gate) -> None:
    changeset = commit(gate)
    source = sorted(path.relative_to(gate.writer).as_posix() for path in (gate.writer / "sources").glob("*.source"))[0]
    tampered = external_push(gate, source, gate.read(source), "tampered evidence\n")
    ledger = git(gate.remote, "show", "main:sources/.hashes.yaml")

    response = revert(gate, {"changeset": changeset["id"]})

    assert response.status_code == 500, response.text
    job = response.json()
    assert (job["status"], job["failure"]["class"], job["failure"]["retryable"]) == ("failed", "input", False)
    assert job["validation"]["status"] == "not_run" and job["validation"]["errors"] == [
        f"{source}: source drift exists before ingest (changed)"]
    assert gate.remote_head() == gate.head() == tampered and git(gate.writer, "status", "--porcelain") == ""
    assert git(gate.remote, "show", "main:sources/.hashes.yaml") == ledger


def test_a_packet_another_concept_still_cites_stays(gate) -> None:
    created = commit(gate, gate.put(PROBE, concept("Probe")))
    later = commit(gate, gate.put(VISITORS, cited(gate.read(VISITORS))))  # the same packet bytes
    assert later["source_snapshot"] == created["source_snapshot"]

    response = revert(gate, {"changeset": created["id"]})

    assert response.status_code == 201, response.text
    assert response.json()["kept_sources"] == [created["source_snapshot"]]
    assert not (gate.writer / PROBE).exists() and (gate.writer / created["source_snapshot"]).is_file()
    assert "The funnel moved." in gate.read(VISITORS)


def test_a_stale_base_refuses_the_revert_as_transient(gate) -> None:
    changeset = commit(gate)
    head = gate.head()
    gate.remote.rename(gate.remote.with_name("away.git"))
    try:
        response = revert(gate, {"changeset": changeset["id"]})
    finally:
        gate.remote.with_name("away.git").rename(gate.remote)

    assert response.status_code == 503 and response.headers["Retry-After"] == "300"
    job = response.json()
    assert (job["status"], job["failure"]["class"], job["failure"]["stage"]) == ("failed", "transient", "pre_sync")
    assert gate.head() == head and git(gate.writer, "status", "--porcelain") == ""
    # A failed revert holds nothing: the same request runs again once the remote is back.
    assert revert(gate, {"changeset": changeset["id"]}).status_code == 201


def test_a_push_race_on_a_reverted_file_is_a_conflict_after_the_recheck(gate) -> None:
    changeset = commit(gate)
    head = gate.head()
    real, pushes = curate._git, []

    def racing(root, *args, **kwargs):
        if args[:1] == ("push",) and not pushes:  # lines far from the revert's edit: Git merges them
            pushes.append(external_push(gate, METRIC, "- Redacted alias 2", "- Redacted alias 2 (v2)"))
        return real(root, *args, **kwargs)

    gate.monkeypatch.setattr(curate, "_git", racing)
    response = revert(gate, {"changeset": changeset["id"]})

    assert response.status_code == 409, response.text
    job = response.json()
    assert (job["status"], job["phase"], job["failure"]["class"]) == ("rejected", "rolled_back", "conflict")
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("conflict", METRIC)]
    assert job["reverted"] == []
    assert gate.remote_head() == pushes[0] and gate.head() == head
    assert "The funnel moved." in git(gate.remote, "show", f"main:{METRIC}")


def test_a_link_pushed_to_a_reverted_concept_meanwhile_is_a_conflict(gate) -> None:
    created = commit(gate, gate.put(PROBE, concept("Probe")))
    head = gate.head()
    real, pushes = curate._git, []

    def racing(root, *args, **kwargs):
        if args[:1] == ("push",) and not pushes:  # Git rebases cleanly; the merged tree has a dead link
            pushes.append(external_push(gate, VISITORS, "# Summary\n\n", "# Summary\n\nSee [Probe](probe.md).\n\n"))
        return real(root, *args, **kwargs)

    gate.monkeypatch.setattr(curate, "_git", racing)
    response = revert(gate, {"changeset": created["id"]})

    assert response.status_code == 409, response.text
    job = response.json()
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("broken_link", VISITORS)]
    assert (job["failure"]["class"], job["phase"]) == ("conflict", "rolled_back")
    assert gate.remote_head() == pushes[0] and gate.head() == head and (gate.writer / PROBE).is_file()


def test_admin_routes_need_the_admin_scope_and_a_named_actor(gate, monkeypatch) -> None:
    changeset = commit(gate)
    for token in ("curator", "auditor", "member"):
        assert revert(gate, {"changeset": changeset["id"]}, token=token).status_code == 403
        assert gate.client.get("/admin/changesets", params={"bundle": "kb-a"},
                               headers=gate.headers(token)).status_code == 403
    assert listing(gate, since="not a time").status_code == 400

    monkeypatch.delenv("AIWIKI_PRINCIPALS")
    gate.app(AIWIKI_TOKEN="aiw_h_owner")  # the legacy shared token holds every scope, but no actor
    shared = revert(gate, {"changeset": changeset["id"]})
    assert shared.status_code == 403 and "no actor" in shared.json()["detail"]
    assert listing(gate).status_code == 200

    gate.app(AIWIKI_DISABLE="admin")
    assert listing(gate).status_code == 403
    assert revert(gate, {"changeset": changeset["id"]}).status_code == 403
    assert [stem for stem in gate.jobs() if gate.job(stem)["kind"] == "revert"] == []


def test_admin_changesets_lists_newest_first_by_principal_and_time(gate) -> None:
    first = commit(gate)
    second = gate.post(gate.request(gate.put(OWNED, concept("Owner note"))), token="owner").json()
    created_at(gate, first["id"], "2020-01-01T10:00:00Z")

    rows = listing(gate).json()
    mine = listing(gate, principal=OPERATOR).json()
    recent = listing(gate, since="2020-01-02T00:00:00Z", limit=5).json()
    capped = listing(gate, limit=1).json()

    assert [row["id"] for row in rows["changesets"]] == [second["id"], first["id"]]
    assert rows["changesets"][1] | {"finished": None} == {
        "id": first["id"], "status": "done", "principal": OPERATOR, "actor": OPERATOR, "run": None,
        "created": "2020-01-01T10:00:00Z", "finished": None, "commit": first["commit"], "noop": None,
        "work_items": [], "concept_files": [METRIC], "deprecated_files": [],
        "changeset_sha256": first["changeset_sha256"], "reverted_by": None}
    assert [row["id"] for row in mine["changesets"]] == [first["id"]]
    assert [row["id"] for row in recent["changesets"]] == [second["id"]]
    assert (capped["shown"], capped["total"], capped["truncated"]) == (1, 2, True)


# --- the revert on the serial worker ----------------------------------------------------------


def test_a_queued_revert_is_queued_again_after_a_restart(gate) -> None:
    changeset = commit(gate)
    job = I.new_revert_job(gate.writer, principal="human:owner", actor="human:owner", run=None, reason=None,
                           selection={"changeset": changeset["id"]},
                           changesets=[{"id": changeset["id"], "commit": changeset["commit"], "principal": OPERATOR}])

    assert worker.recover([gate.writer]) is True

    wait_for(lambda: gate.job(job["id"])["status"] == "done")
    assert gate.job(job["id"])["reverted"] == [changeset["id"]] and gate.remote_head() == gate.job(job["id"])["commit"]


class _Killed(BaseException):
    """The process dies here: not even the job's own failure handling runs."""


@pytest.mark.parametrize("pushed", [False, True])
def test_a_revert_killed_around_its_push_is_reconciled_on_restart(gate, pushed) -> None:
    changeset = commit(gate)
    head = gate.head()
    job = I.new_revert_job(gate.writer, principal="human:owner", actor="human:owner", run=None, reason=None,
                           selection={"changeset": changeset["id"]},
                           changesets=[{"id": changeset["id"], "commit": changeset["commit"], "principal": OPERATOR}])
    real = curate._git

    def killed(root, *args, **kwargs):
        if args[:1] == ("push",):
            if pushed:
                real(root, *args, **kwargs)
            raise _Killed
        return real(root, *args, **kwargs)

    gate.monkeypatch.setattr(curate, "_git", killed)
    with pytest.raises(_Killed):
        revert_runtime.run(gate.writer, I.job_path(gate.writer, job["id"]))
    gate.monkeypatch.setattr(curate, "_git", real)
    assert (gate.job(job["id"])["status"], gate.job(job["id"])["phase"]) == ("running", "committed")

    assert worker.recover([gate.writer]) is True

    recovered = gate.job(job["id"])
    assert git(gate.writer, "status", "--porcelain") == ""
    if pushed:
        assert (recovered["status"], recovered["recovered"]) == ("done", "remote_contains_commit")
        assert gate.remote_head() == recovered["commit"] == gate.head()
        assert revert(gate, {"changeset": changeset["id"]}).json()["id"] == job["id"]  # never reverted twice
    else:
        assert (recovered["status"], recovered["phase"], recovered["reverted"]) == ("failed", "rolled_back", [])
        assert recovered["failure"]["class"] == "interrupted" and gate.head() == gate.remote_head() == head
        assert revert(gate, {"changeset": changeset["id"]}).status_code == 201  # a rollback holds nothing


def test_a_deferred_codex_audit_of_a_reverted_changeset_never_runs(gate) -> None:
    lease = gate.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run="WAIO-9"))
    assert lease.status_code == 200
    changeset = commit(gate)
    assert changeset["audit"]["deferred_until"] == "lease_release"

    assert revert(gate, {"changeset": changeset["id"]}).status_code == 201
    gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run="WAIO-9"))

    wait_for(lambda: gate.job(changeset["audit"]["job"])["status"] == "failed")
    audit = gate.job(changeset["audit"]["job"])
    assert gate.audits == [] and (audit["failure"]["class"], audit["failure"]["retryable"]) == ("input", False)
    assert I.pending_audits(gate.writer, older_than_hours=0)["total"] == 0
    # The watchdog reads the revert as what settled that audit, not as a failure to page about.
    watchdog = subprocess.run([sys.executable, str(WATCHDOG), "--bundle", str(gate.writer)],
                              capture_output=True, text=True, timeout=60)
    report = json.loads(watchdog.stdout)
    assert (watchdog.returncode, report["alerts"]) == (0, []), report
    assert [(row["id"], row["resolved_by"]) for row in report["checks"]["writer:kb-a"]["failed_in_window"]] == [
        (audit["id"], I.reverted_by(gate.writer)[changeset["id"]])]


# --- earlier published revisions for admin compare -----------------------------------------


def test_workspace_serves_an_earlier_published_revision(gate) -> None:
    head = gate.head()
    commit(gate)

    def fetch(revision: str):
        return gate.client.get("/workspace", params={"bundle": "kb-a", "revision": revision},
                               headers=gate.headers("owner"))

    earlier = fetch(head[:12])
    assert earlier.status_code == 200 and earlier.headers["X-AIWiki-Revision"] == head
    # History still holds what a revert took out: a reader gets the published head only.
    for token in ("member", "curator"):
        assert gate.client.get("/workspace", params={"bundle": "kb-a", "revision": head},
                               headers=gate.headers(token)).status_code == 403
        assert gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers(token)).status_code == 200
    with tarfile.open(fileobj=io.BytesIO(earlier.content)) as tree:
        text = tree.extractfile(METRIC).read().decode()
    assert text == git(gate.writer, "show", f"{head}:{METRIC}") + "\n" and "The funnel moved." not in text
    unpushed = gate.writer / "unpushed.txt"
    unpushed.write_text("local\n", encoding="utf-8")
    git(gate.writer, "add", "unpushed.txt")
    git(gate.writer, "commit", "-qm", "local only")
    for revision in (gate.head(), "0" * 40, "HEAD", "--all"):
        assert fetch(revision).status_code == 404, revision
    git(gate.writer, "reset", "-q", "--hard", "HEAD~1")
    assert json.loads(fetch("HEAD").text)["detail"].endswith("is not a published revision of this bundle")

    # The shared legacy token holds admin but names nobody: it reads the published head only.
    gate.monkeypatch.delenv("AIWIKI_PRINCIPALS")
    gate.app(AIWIKI_TOKEN="aiw_h_owner")
    shared = gate.client.get("/workspace", params={"bundle": "kb-a", "revision": head[:12]},
                             headers=gate.headers("owner"))
    assert shared.status_code == 403 and "no actor" in shared.json()["detail"]
    assert gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("owner")).status_code == 200
