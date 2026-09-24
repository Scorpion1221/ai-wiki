"""POST /changesets and GET /workspace on the writer (design §2.1–§2.11, acceptance §10.1).

Scope and limits (403/413/429), idempotency, dry-run, 202 behind a Codex pass, queue
priority, Codex audits deferred while a maintainer run lasts, recovery from
``.okf/changesets``, and receipts that the legacy readers (``maintain.receipt``,
``audit.concept_files``, ``audit._find_source``) accept unchanged.
"""
from __future__ import annotations

import base64
import io
import json
import tarfile
import threading
from datetime import UTC, datetime, timedelta

import pytest
from gate_fixture import (
    AI_STUDY,
    AIO_AB,
    CURATOR,
    EVIDENCE,
    METRIC,
    SPECS,
    TOKENS,
    Gate,
    cite,
    cited,
    concept,
    git,
    wait_for,
)

from aiwiki.cli.maintain import receipt
from aiwiki.engine.document import parse_document
from aiwiki.runtime import audit, changeset
from aiwiki.service import auth, worker
from aiwiki.service import ingest as I
from aiwiki.service import maint_state as M

VIEWS = "metrics/view-references-exposure-proxy-2026-09.md"
VISITORS = "metrics/web-landing-new-visitor-distribution-2026-09.md"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    yield gate
    gate.close()


# --- receipts ----------------------------------------------------------------------------


def test_a_committed_changeset_receipt_reads_like_an_ingest(gate) -> None:
    head = gate.head()
    item_id = gate.item(run="WAIO-612")

    response = gate.post(gate.item_request(item_id), run="WAIO-612")

    assert response.status_code == 201, response.text
    job = response.json()
    assert (job["kind"], job["mode"], job["status"], job["deduplicated"]) == ("ingest", "changeset", "done", False)
    assert (job["principal"], job["actor"], job["run"]) == (CURATOR, CURATOR, "WAIO-612")
    assert job["work_items"] == job["closed_items"] == [item_id]
    assert job["commit"] == gate.remote_head() and git(gate.writer, "rev-parse", "HEAD~1") == head
    assert job["concept_files"] == [METRIC] and job["validation"]["status"] == "passed"
    assert gate.client.get(f"/jobs/{job['id']}", params={"bundle": "kb-a"},
                           headers=gate.headers()).json() == gate.job(job["id"])
    # The pre-changeset readers take it as they are.
    receipt(job)
    assert audit.concept_files(gate.writer, job) == [METRIC]
    assert audit._find_source(gate.writer, job) == job["source_snapshot"]
    assert (gate.writer / job["source_snapshot"]).read_bytes() == EVIDENCE
    frontmatter = parse_document(gate.read(METRIC)).frontmatter
    assert frontmatter["generated"]["by"] == CURATOR
    assert frontmatter["sources"][-1]["resource"] == "/" + job["source_snapshot"]
    # Phases 1-3: a Codex audit of the changeset is registered with it and runs on the worker
    # once the run releases its lease.
    audit_job = gate.job(job["audit"]["job"])
    assert job["audit"] == {"mode": "codex", "job": audit_job["id"], "deferred_until": "lease_release"}
    assert (audit_job["kind"], audit_job["parent_job"], audit_job["concept_files"]) == ("audit", job["id"], [METRIC])
    gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run="WAIO-612"))
    wait_for(lambda: gate.audits == [job["id"]])
    assert I.pending_audits(gate.writer, older_than_hours=0)["total"] == 0


def test_a_noop_changeset_is_a_200_receipt_without_a_commit(gate) -> None:
    head = gate.head()

    response = gate.post(gate.request(gate.put(METRIC, gate.read(METRIC))))

    assert response.status_code == 200, response.text
    job = response.json()
    assert (job["status"], job["noop"], job["commit"], job["concept_files"]) == ("done", True, None, [])
    receipt(job)  # documented no-change exception
    gate.assert_untouched(head)
    assert gate.job(job["audit"]["job"])["reason"] == "no_concepts_to_audit"


def test_an_upload_of_a_packets_bytes_is_its_own_ingest(gate) -> None:
    curated = []
    gate.monkeypatch.setattr(worker.curate, "run", lambda _bundle, source, _job_path: curated.append(source))
    noop = gate.post(gate.request(gate.put(METRIC, gate.read(METRIC)))).json()
    assert noop["noop"] is True  # its packet was never stored

    member = gate.client.post("/ingest", params={"bundle": "kb-a"}, headers=gate.headers("member"),
                              json={"content_b64": base64.b64encode(EVIDENCE).decode(), "filename": "status.md"})

    job = member.json()
    assert member.status_code == 200 and job["deduplicated"] is False and "mode" not in job
    assert (gate.writer / job["source"]).read_bytes() == EVIDENCE
    worker._q.join()
    assert curated == [job["source"]]  # the Codex curator gets the member's bytes


def test_external_audit_mode_registers_no_codex_audit(gate) -> None:
    gate.monkeypatch.setenv("AIWIKI_AUDIT", "external")

    job = gate.post(gate.request()).json()

    assert job["status"] == "done" and job["audit"] == {"mode": "external"}
    assert [stem for stem in gate.jobs() if gate.job(stem)["kind"] == "audit"] == []



def test_a_manual_audit_bundle_queues_its_codex_audit_only_on_request(gate) -> None:
    # Phase 2 (design §9): the shadow bundle leaves its audits to the admin cron, so it spends
    # none of production's quota by itself, and a requested one keeps its FIFO place.
    gate.app(AIWIKI_CODEX_AUDIT_MANUAL="kb-a")
    assert worker.COMMIT_BUNDLES == {"kb-a"} and worker.AUDIT_BUNDLES == frozenset()

    job = gate.post(gate.request()).json()

    assert job["status"] == "done" and job["audit"] == {"mode": "manual"}
    assert [stem for stem in gate.jobs() if gate.job(stem)["kind"] == "audit"] == []
    assert [row["id"] for row in I.pending_audits(gate.writer, older_than_hours=0)["jobs"]] == [job["id"]]
    requested = gate.client.post(f"/jobs/{job['id']}/audit", params={"bundle": "kb-a"}, headers=gate.headers("owner"))
    assert requested.status_code == 200 and requested.json()["status"] == "queued"
    wait_for(lambda: gate.audits == [job["id"]])

# --- G0 scope, G1 limits, G2 quotas -----------------------------------------------------


def test_scopes_bundles_and_the_commit_allow_list(gate) -> None:
    request = gate.request()
    audit_request = {"schema": "ai-wiki.changeset/v1", "kind": "audit", "base_revision": gate.head(),
                     "reviews": [{"path": METRIC, "base": request["files"][0]["base"], "verdict": "verified"}]}

    for token, body in (("member", request), ("auditor", request), ("curator", audit_request)):
        denied = gate.post(body, token=token)
        assert denied.status_code == 403 and "lacks scope" in denied.json()["detail"], token
    actorless = gate.post(request, token="actorless")  # never stamp a member into generated.by
    assert actorless.status_code == 403 and "no actor" in actorless.json()["detail"]
    assert gate.post(gate.request(bundle="kb-c"), bundle="kb-c").status_code == 403  # not its bundle
    only_dry = gate.post(gate.request(bundle="kb-b"), bundle="kb-b")
    assert only_dry.status_code == 403 and "AIWIKI_CHANGESETS_COMMIT" in only_dry.json()["detail"]
    assert gate.post(gate.request(bundle="kb-b"), bundle="kb-b", dry_run=True).status_code == 200
    # An audit changeset passes G0 with the audit scope, but its gate opens in phase 4.
    later = gate.post(audit_request, token="auditor")
    assert later.status_code == 400 and later.json()["errors"][0]["code"] == "input"
    assert gate.jobs() == []

    gate.app(AIWIKI_DISABLE="changesets,workspace")
    assert gate.post(request).status_code == 403
    assert gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers()).status_code == 403
    gate.app(AIWIKI_CURATE="off")  # a read mirror never commits, but may still judge
    assert gate.post(request).status_code == 403
    assert gate.post(request, dry_run=True).status_code == 200


def test_malformed_requests_are_400_before_any_job(gate) -> None:
    for body in (None, [], {"kind": ["curate"]}, {**gate.request(), "files": "all"}, {**gate.request(), "x": 1}):
        response = gate.post(body)
        assert response.status_code == 400 and response.json()["errors"][0]["code"] == "input", body
    # A body cut off in transit is bad input, never a 422 held against the agent, and only
    # after the token check.
    truncated = json.dumps(gate.request())[:-40].encode()
    anonymous = gate.client.post("/changesets", params={"bundle": "kb-a"}, content=truncated,
                                 headers={"Content-Type": "application/json"})
    cut = gate.client.post("/changesets", params={"bundle": "kb-a"}, content=truncated,
                           headers={**gate.headers(), "Content-Type": "application/json"})
    assert anonymous.status_code == 401
    assert cut.status_code == 400 and cut.json()["errors"][0]["code"] == "input"
    assert cut.json()["failure"]["class"] == "input"
    assert gate.jobs() == []


def test_limits_are_413_before_any_job(gate) -> None:
    files = [gate.put(f"metrics/probe-{index}.md", concept(f"Probe {index}")) for index in range(21)]

    response = gate.post(gate.request(*files))

    assert response.status_code == 413
    assert {error["code"] for error in response.json()["errors"]} == {"too_large"}
    # The evidence packet is built and measured before a job exists too.
    oversized = gate.post(gate.request(evidence=b"x" * (1024 * 1024 + 10)))
    empty = gate.post(gate.request(evidence=b"  \n"))
    assert oversized.status_code == 413 and oversized.json()["errors"][0]["code"] == "too_large"
    assert empty.status_code == 400 and "is empty" in empty.json()["errors"][0]["message"]
    assert gate.jobs() == [] and not (gate.writer / ".okf" / "changesets").exists()


def test_quotas_are_429_with_retry_after_and_a_resend_is_still_answered(gate) -> None:
    gate.app(AIWIKI_CHANGESETS_PER_HOUR="2")
    request = gate.request()
    done = gate.post(request)
    assert done.status_code == 201
    stale = gate.request(gate.put(AIO_AB, cited(gate.read(AIO_AB))))
    stale["files"][0]["base"] = "ch1:" + "0" * 64
    assert gate.post(stale).status_code == 409  # a rejection counts toward the quota too

    third = gate.post(gate.request(gate.put("metrics/probe.md", concept("Probe"))))

    assert third.status_code == 429 and 3000 < int(third.headers["Retry-After"]) <= 3600
    assert third.json()["errors"][0] | {"message": ""} == {"code": "rate_limited", "limit": "changesets_per_hour",
                                                            "message": ""}
    assert len(gate.jobs()) == 3  # the two changesets and the first one's audit
    resend = gate.post({**request, "base_revision": done.json()["commit"]})
    assert resend.status_code == 200 and resend.json()["id"] == done.json()["id"]


def test_deprecations_have_their_own_daily_quota(gate) -> None:
    # The curator's principal may deprecate 2 concepts a day.
    first = gate.post(gate.request(gate.put(METRIC, cited(gate.read(METRIC))), gate.deprecate(AI_STUDY)))
    assert first.status_code == 201, first.text

    probe = gate.put("metrics/probe.md", concept("Probe"))
    over = gate.post(gate.request(probe, gate.deprecate(AIO_AB, "metrics/probe.md"),
                                  gate.deprecate(VISITORS, "metrics/probe.md")))

    assert over.status_code == 429 and over.json()["errors"][0]["limit"] == "deprecations_per_day"
    one_more = gate.post(gate.request(probe, gate.deprecate(VISITORS, "metrics/probe.md")))
    assert one_more.status_code == 201, one_more.text


def test_a_deprecation_retry_after_waits_for_deprecations_to_age_out(gate) -> None:
    # A changeset that deprecated nothing, a day old but for an hour, frees no deprecation.
    first = gate.post(gate.request())
    assert first.status_code == 201
    stored = gate.job(first.json()["id"])
    stored["created"] = (datetime.now(UTC) - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
    I.job_path(gate.writer, stored["id"]).write_text(json.dumps(stored), encoding="utf-8")
    probe = gate.put("metrics/probe.md", concept("Probe"))
    two = gate.post(gate.request(probe, gate.deprecate(AI_STUDY, "metrics/probe.md"),
                                 gate.deprecate(AIO_AB, "metrics/probe.md")))
    assert two.status_code == 201, two.text

    over = gate.post(gate.request(gate.put(METRIC, cited(gate.read(METRIC), "Other claim.")),
                                  gate.deprecate(VISITORS)))

    assert over.status_code == 429 and over.json()["errors"][0]["limit"] == "deprecations_per_day"
    assert 23 * 3600 < int(over.headers["Retry-After"]) <= 24 * 3600


# --- G3 idempotency ------------------------------------------------------------------------


def test_the_same_content_on_a_newer_base_returns_the_done_job(gate) -> None:
    request = gate.request()
    first = gate.post(request).json()
    assert first["status"] == "done"

    again = gate.post({**request, "base_revision": first["commit"], "run": "WAIO-613", "message": "retry"})

    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert again.json()["id"] == first["id"] and gate.remote_head() == first["commit"]



def test_a_receipt_names_the_authored_base_and_the_head_it_was_applied_on(gate) -> None:
    authored = gate.head()
    views = gate.request(gate.put(VIEWS, cite(gate.read(VIEWS), "views-status")), evidence=b"Views moved.\n")
    views["evidence"]["id"] = "views-status"
    first = gate.post(gate.request()).json()  # another changeset lands first

    applied = gate.post(views).json()

    assert applied["status"] == "done", applied
    assert applied["request_base_revision"] == authored != first["commit"]
    assert applied["base_revision"] == first["commit"]  # the file-level rebase of design §2.6

def test_a_rejected_changeset_frees_its_key_for_a_fixed_resubmission(gate) -> None:
    request = gate.request()
    stale = {**request, "files": [{**request["files"][0], "base": "ch1:" + "0" * 64}]}
    rejected = gate.post(stale)
    assert rejected.status_code == 409 and rejected.json()["errors"][0]["code"] == "conflict"
    assert gate.job(rejected.json()["id"])["status"] == "rejected"  # persisted for the rejection rate

    fixed = gate.post(request)  # the base is not part of the key: the same key, now free

    assert fixed.status_code == 201 and fixed.json()["id"] != rejected.json()["id"]
    assert fixed.json()["changeset_sha256"] == rejected.json()["changeset_sha256"]


def test_a_closed_work_item_takes_no_further_changeset(gate) -> None:
    item_id = gate.item()
    request = gate.item_request(item_id)
    first = gate.post(request, run="WAIO-1")
    assert first.status_code == 201, first.text
    assert first.json()["closed_items"] == [item_id]
    item = M.get_item(gate.writer, item_id)
    assert (item["status"], item["resolution"]["job"]) == ("curated", first.json()["id"])

    other = gate.item_request(item_id, gate.put(VIEWS, cited(gate.read(VIEWS))))
    closed = gate.post(other, run="WAIO-1")

    assert closed.status_code == 409
    assert closed.json()["errors"][0] | {"message": ""} == {
        "code": "work_item_closed", "item": item_id, "closed_by": first.json()["id"], "message": ""}
    # A resend of the changeset that closed it still gets its receipt.
    assert gate.post(request, run="WAIO-1").json()["id"] == first.json()["id"]


def test_the_run_header_is_the_one_run_of_the_changeset(gate) -> None:
    head = gate.head()
    mismatch = gate.post(gate.request(run="WAIO-999"), run="WAIO-1")
    malformed = gate.post(gate.request(), run="WAIO 1\nRun: forged")
    secret = gate.post(gate.request(), run="ghp_" + "a1" * 18)
    for refused in (mismatch, malformed):
        assert refused.status_code == 400 and refused.json()["errors"][0]["code"] == "input"
    assert secret.status_code == 422 and secret.json()["errors"][0]["code"] == "secret_detected"
    assert "a1a1" not in secret.text
    assert gate.jobs() == []
    gate.assert_untouched(head)

    done = gate.post(gate.request(), run="WAIO-612")  # the header alone names the run

    assert done.status_code == 201, done.text
    assert done.json()["run"] == "WAIO-612"
    assert "\nRun: WAIO-612" in git(gate.writer, "log", "-1", "--format=%B")


def test_work_items_need_the_run_that_holds_the_lease(gate) -> None:
    item_id = gate.item(run="WAIO-1")
    head = gate.head()

    for run in (None, "WAIO-2"):
        refused = gate.post(gate.item_request(item_id), run=run)
        assert refused.status_code == 409 and refused.json()["errors"][0]["code"] == "lease_required", run
    unknown = gate.post(gate.item_request("it_000000000000"), run="WAIO-1")

    assert unknown.status_code == 400
    gate.assert_untouched(head)
    assert gate.jobs() == []
    # A changeset of the run renews its lease.
    later = M._now() + timedelta(hours=2)
    gate.monkeypatch.setattr(M, "_now", lambda: later)
    assert gate.post(gate.item_request(item_id), run="WAIO-1").status_code == 201
    assert M.active_lease(gate.writer, "maintainer")["expires_at"] == M._iso(later + M.LEASE_TTL)


# --- dry-run ---------------------------------------------------------------------------------


def test_a_dry_run_writes_no_job_takes_no_lock_and_leaves_git_alone(gate) -> None:
    head, refs = gate.head(), git(gate.writer, "for-each-ref")
    request = gate.request()

    with worker.serialized_mutation():  # a Codex pass holds the writer lock
        response = gate.post(request, dry_run=True)
        uncited = gate.post(gate.request(gate.put(VIEWS, gate.read(VIEWS) + "\nNew claim.\n")), dry_run=True)
        unknown = gate.post({**request, "base_revision": "0" * 40}, dry_run=True)

    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["status"], result["dry_run"], result["published_revision"]) == ("would_apply", True, head)
    assert "files" not in result and "id" not in result
    assert result["diffs"][METRIC].startswith(f"--- a/{METRIC}\n+++ b/{METRIC}\n")
    assert "+The funnel moved.[^funnel-status-2026-09-24]" in result["diffs"][METRIC]
    assert uncited.status_code == 422 and {e["code"] for e in uncited.json()["errors"]} == {"uncited_change"}
    assert unknown.status_code == 409 and unknown.json()["errors"][0]["code"] == "unknown_base"
    assert gate.jobs() == [] and not (gate.writer / ".okf" / "changesets").exists()
    gate.assert_untouched(head)
    assert git(gate.writer, "for-each-ref") == refs


# --- queueing ----------------------------------------------------------------------------


def test_202_behind_a_codex_pass_and_changesets_run_before_queued_codex_work(gate) -> None:
    gate.app(AIWIKI_CHANGESET_WAIT_S="0.3")
    release, order = threading.Event(), []

    def codex(_bundle, source, _job_path):
        if source == "codex-a":
            release.wait(10)
        order.append((source, [gate.job(stem)["status"] for stem in gate.jobs()
                               if gate.job(stem).get("mode") == "changeset"]))

    gate.monkeypatch.setattr(worker.curate, "run", codex)
    worker.ensure_started()
    worker.submit(gate.writer, "codex-a", gate.tmp / "codex-a.json")
    wait_for(worker.is_mutating)
    worker.submit(gate.writer, "codex-b", gate.tmp / "codex-b.json")

    response = gate.post(gate.request())

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued" and response.headers["Location"] == (
        f"/jobs/{response.json()['id']}?bundle=kb-a")
    polled = gate.client.get(response.headers["Location"], headers=gate.headers())
    assert polled.json()["status"] == "queued"
    release.set()
    worker._q.join()
    # The running Codex pass finished; the changeset went ahead of the Codex ingest queued before it.
    assert order == [("codex-a", ["queued"]), ("codex-b", ["done"])]
    assert gate.job(response.json()["id"])["commit"] == gate.remote_head()


def test_a_resend_of_a_queued_changeset_answers_at_once(gate) -> None:
    waits = []
    gate.monkeypatch.setattr(worker, "wait", lambda job_path, timeout: waits.append(job_path.stem))
    request = gate.request()

    with worker.serialized_mutation():  # a Codex pass holds the writer lock
        first = gate.post(request)
        resend = gate.post(request)

    assert (first.status_code, resend.status_code) == (202, 202)
    assert resend.json()["id"] == first.json()["id"] and resend.json()["deduplicated"] is True
    assert resend.headers["Location"] == first.headers["Location"]
    assert waits == [first.json()["id"]]  # only the request that queued it waited for it


def test_codex_audits_wait_for_the_maintainer_run(gate) -> None:
    item_id = gate.item(run="WAIO-1")

    response = gate.post(gate.item_request(item_id), run="WAIO-1")

    job = response.json()
    assert response.status_code == 201, response.text
    assert job["audit"] == {"mode": "codex", "job": job["audit"]["job"], "deferred_until": "lease_release"}
    threading.Event().wait(0.3)  # many deferral polls later
    assert gate.audits == [] and M.status(gate.writer)["audit"]["queued"] == 1

    released = gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"},
                                  headers=gate.headers(run="WAIO-1"))

    assert released.json()["released"] is True
    wait_for(lambda: gate.audits == [job["id"]])


# --- rollback --------------------------------------------------------------------------


@pytest.mark.parametrize("stop", ["rollback", "revoked"])
def test_a_queued_changeset_is_stopped_by_a_rollback_or_a_revoked_principal(gate, stop) -> None:
    gate.app(AIWIKI_CHANGESET_WAIT_S="0.2")
    head = gate.head()
    item_id = gate.item(run="WAIO-1")
    request = gate.item_request(item_id)
    with worker.serialized_mutation():  # a Codex pass holds the writer lock; the changeset queues
        queued = gate.post(request, run="WAIO-1")
        assert queued.status_code == 202, queued.text
        if stop == "rollback":  # §2.11: clear the allow-list, disable the route, restart
            gate.app(AIWIKI_CHANGESETS_COMMIT="", AIWIKI_DISABLE="changesets")
            assert worker.recover([gate.writer]) is True
        else:  # §8.5 step 1: remove the principal, then SIGHUP
            (gate.tmp / "principals.json").write_text(json.dumps({"principals": [
                {**spec, "token_sha256": auth.token_sha256(TOKENS[name])} for name, spec in SPECS.items()
                if name != "curator"]}), encoding="utf-8")
            assert gate.appmod.AUTH.reload() is True
    worker._q.join()

    job = gate.job(queued.json()["id"])
    assert (job["status"], job["http_status"], job["failure"]["class"]) == ("rejected", 403, "auth")
    assert job["errors"][0]["code"] == "forbidden"
    gate.assert_untouched(head)
    assert not I.changeset_path(gate.writer, job["id"]).exists()



def test_a_queued_changeset_runs_as_admitted_whatever_is_rewritten_in_okf(gate) -> None:
    """An in-place Codex audit can write .okf while a changeset waits for the lock (design
    §8.3): the actor comes from the principal in force, and a rewritten request never runs."""
    gate.app(AIWIKI_CHANGESET_WAIT_S="0.2")
    item_id = gate.item(run="WAIO-1")
    head = gate.head()
    with worker.serialized_mutation():  # the Codex pass holds the writer lock; both changesets queue
        stamped = gate.post(gate.item_request(item_id, close_items=False), run="WAIO-1").json()
        forged = gate.post(gate.item_request(item_id, gate.put(VIEWS, cited(gate.read(VIEWS)))), run="WAIO-1").json()
        assert (stamped["status"], forged["status"]) == ("queued", "queued")
        for job, change in ((stamped, {"actor": "process:ai-wiki-auditor"}),
                            (forged, {"request": {**gate.item_request(item_id, gate.put(
                                AIO_AB, cited(gate.read(AIO_AB)))), "run": "WAIO-1"}})):
            path = I.changeset_path(gate.writer, job["id"])
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **change}), encoding="utf-8")
        record = gate.job(forged["id"])
        I.job_path(gate.writer, forged["id"]).write_text(json.dumps({**record, "principal": "human:owner"}),
                                                          encoding="utf-8")
    for job in (stamped, forged):  # the lease still holds the stamped changeset's audit back
        worker.wait(I.job_path(gate.writer, job["id"]), 30)

    done, refused = gate.job(stamped["id"]), gate.job(forged["id"])
    assert done["status"] == "done" and done["actor"] == CURATOR
    assert parse_document(gate.read(METRIC)).frontmatter["generated"]["by"] == CURATOR
    assert (refused["status"], refused["failure"]["class"]) == ("failed", "internal")
    assert "no longer matches what was admitted" in refused["error"]
    assert git(gate.remote, "diff", "--name-only", head, "main").count(AIO_AB) == 0

# --- recovery ----------------------------------------------------------------------------


def test_recovery_runs_a_queued_changeset_from_its_persisted_request(gate) -> None:
    item_id = gate.item(run="WAIO-1")
    request = gate.item_request(item_id)
    _meta, data = M.evidence(gate.writer, item_id, "S1-status.md")
    record = {"request": request, "actor": CURATOR, "evidence_files": [
        {"name": f"{item_id}/S1-status.md", "sha256": _meta["sha256"], "origin": _meta["origin"]}]}
    digest = changeset.changeset_sha256(request, {f"{item_id}/S1-status.md": _meta["sha256"]})
    job = I.new_changeset_job(gate.writer, record, principal=CURATOR, actor=CURATOR, run="WAIO-1",
                              work_items=[item_id], close_items=True, changeset_sha256=digest, deprecations=0)

    assert worker.recover([gate.writer]) is True
    worker.wait(I.job_path(gate.writer, job["id"]), 30)

    done = gate.job(job["id"])
    assert done["status"] == "done" and done["commit"] == gate.remote_head()
    assert done["closed_items"] == [item_id] and M.get_item(gate.writer, item_id)["status"] == "curated"
    assert not I.changeset_path(gate.writer, job["id"]).exists()  # a finished job needs no request


def test_recovery_after_the_items_were_closed_still_reports_them(gate) -> None:
    item_id = gate.item(run="WAIO-1")
    job = gate.post(gate.item_request(item_id), run="WAIO-1").json()
    assert job["closed_items"] == [item_id]
    # As if the writer died in G15 after closing the item but before the receipt said so.
    stored = {key: value for key, value in gate.job(job["id"]).items() if key != "closed_items"}
    I.job_path(gate.writer, job["id"]).write_text(json.dumps(stored), encoding="utf-8")

    assert worker.recover([gate.writer]) is True

    assert gate.job(job["id"])["closed_items"] == [item_id]
    assert M.get_item(gate.writer, item_id)["resolution"]["job"] == job["id"]


def test_recovery_finishes_the_closeout_of_a_done_changeset(gate) -> None:
    item_id = gate.item(run="WAIO-1")
    gate.monkeypatch.setenv("AIWIKI_AUDIT", "external")
    job = gate.post(gate.item_request(item_id, close_items=False), run="WAIO-1").json()
    assert job["status"] == "done" and M.get_item(gate.writer, item_id)["status"] == "in_progress"
    # As if the writer died inside G15: done, but its item still open and no audit registered.
    stored = {key: value for key, value in gate.job(job["id"]).items() if key not in ("closed_items", "audit")}
    I.job_path(gate.writer, job["id"]).write_text(json.dumps({**stored, "close_items": True}), encoding="utf-8")
    gate.monkeypatch.delenv("AIWIKI_AUDIT")

    assert worker.recover([gate.writer]) is True

    finished = gate.job(job["id"])
    assert finished["closed_items"] == [item_id] and M.get_item(gate.writer, item_id)["status"] == "curated"
    assert finished["audit"] | {"job": None} == {"mode": "codex", "job": None, "deferred_until": "lease_release"}
    assert gate.job(finished["audit"]["job"])["parent_job"] == job["id"]
    gate.client.delete("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=gate.headers(run="WAIO-1"))
    wait_for(lambda: gate.audits == [job["id"]])


# --- workspace ---------------------------------------------------------------------------


def test_workspace_serves_the_published_tree_without_viz(gate) -> None:
    published = gate.head()
    (gate.writer / METRIC).write_text("uncommitted edit\n", encoding="utf-8")  # never served

    response = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("member"))

    assert response.status_code == 200 and response.headers["content-type"] == "application/gzip"
    assert response.headers["X-AIWiki-Revision"] == published and response.headers["ETag"] == f'"{published}"'
    with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
        names = archive.getnames()
        metric = archive.extractfile(METRIC).read().decode()
    assert METRIC in names and "index.md" in names and any(name.startswith("sources/") for name in names)
    assert "viz.html" not in names and metric == git(gate.writer, "show", f"HEAD:{METRIC}") + "\n"
    assert gate.client.get("/workspace", params={"bundle": "kb-a"}, headers={
        **gate.headers(), "If-None-Match": f'"{published}"'}).status_code == 304
    denied = gate.client.get("/workspace", params={"bundle": "kb-c"}, headers=gate.headers())
    assert denied.status_code == 403
