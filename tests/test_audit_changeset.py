"""External audit (design §2.3, §5.3–§5.5, acceptance §10.1 ``test_audit_changeset.py``).

The writer derives the audit backlog and judges audit changesets (A1–A8) with no agent: an
auditor anywhere reads GET /audit/backlog and proposes verdicts; the service stamps
``verified``, ``generated`` and ``status``. The real outputs of the failed Codex audits
71ea85ca9c20 and e94c8b707aea are replayed as audit changesets.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gate_fixture import AI_STUDY, AIO_AB, METRIC, Gate, clone, concept, git, wait_for

from aiwiki.cli import main as cli
from aiwiki.cli import workspace
from aiwiki.engine.document import parse_document
from aiwiki.engine.validate import body_spill_errors
from aiwiki.runtime import audit, changeset
from aiwiki.service import auth, worker
from aiwiki.service import ingest as I

AUDITOR = "process:ai-wiki-auditor"
EPOCH = "2026-09-20T00:00:00Z"  # after every fixture generation but AIO_AB's (2026-09-23T15:46Z)
ORPHAN = f"  - {{by: {audit.AUDITOR}, at: 2026-09-19T20:56:59Z}}"
RUN = "AUD-1"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH)
    yield gate
    gate.close()


def lease(gate: Gate, token: str = "auditor", run: str = RUN) -> None:
    response = gate.client.post("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers(token, run))
    assert response.status_code == 200, response.text


def review(gate: Gate, rel: str, verdict: object = "verified", **extra) -> dict:
    """A review of the published version, which the writer syncs to before it judges."""
    base = changeset.content_hash(git(gate.remote, "show", f"main:{rel}"))
    return {"path": rel, "base": base, "verdict": verdict, "note": "every claim matches the cited packet", **extra}


def submit(gate: Gate, *reviews: dict, token: str = "auditor", run: str | None = RUN, dry_run: bool = False):
    request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": gate.head(), "reviews": list(reviews)}
    return gate.post(request, token=token, run=run, dry_run=dry_run)


def backlog(gate: Gate, token: str = "auditor") -> dict:
    response = gate.client.get("/audit/backlog", params={"bundle": "kb-a"}, headers=gate.headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def due(gate: Gate) -> list[tuple[str, str]]:
    return [(entry["path"], entry["reason"]) for entry in backlog(gate)["concepts"]]


def published(gate: Gate, rel: str) -> dict:
    return parse_document(git(gate.remote, "show", f"main:{rel}") + "\n").frontmatter


def push(gate: Gate, rel: str, text: str, message: str = "hand edit") -> str:
    """Someone pushes past the writer (no service trailer); the writer fetches it."""
    other = clone(gate.remote, gate.tmp / f"other-{len(list(gate.tmp.glob('other-*')))}")
    (other / rel).parent.mkdir(parents=True, exist_ok=True)
    (other / rel).write_text(text, encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", message)
    git(other, "push", "-q", "origin", "main")
    git(gate.writer, "fetch", "-q")
    return git(other, "rev-parse", "HEAD")


def narrowed(text: str) -> str:
    return text.replace("Redacted fixture body.", "Redacted body.")


def later(monkeypatch, hours: float, *modules) -> None:
    """The writer's trusted clock (and the review CLI's) ``hours`` ahead: A4's delay passes."""
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=hours)
    for module in (audit, *modules):
        monkeypatch.setattr(module, "datetime", Later)


# --- the backlog (design §5.3) --------------------------------------------------------------------


def test_the_backlog_lists_unverified_generations_since_the_epoch(gate) -> None:
    found = backlog(gate)

    assert [(entry["path"], entry["reason"]) for entry in found["concepts"]] == [(AIO_AB, "generation")]
    entry = found["concepts"][0]
    assert entry["base"] == changeset.content_hash(gate.read(AIO_AB))
    assert entry["generated"] == {"by": "process:ai-wiki-curator", "at": "2026-09-23T15:46:00Z"}
    assert entry["verification_current"] is False and entry["status"] == "draft"
    assert entry["sources"] and all(rel.startswith("sources/") for rel in entry["sources"])
    assert (found["mode"], found["epoch"], found["revision"]) == ("external", EPOCH, gate.remote_head())
    assert (found["shown"], found["total"], found["truncated"]) == (1, 1, False)
    # Nothing a curator wrote to hand the audit over is part of it.
    assert set(entry) == {"path", "base", "type", "title", "status", "generated", "verification_current",
                          "sources", "reason", "since"}
    assert entry["since"] == entry["generated"]["at"]


def test_the_backlog_is_derived_so_any_rebuild_agrees(gate) -> None:
    lease(gate)
    assert submit(gate, review(gate, AIO_AB, "unverified")).status_code == 201
    push(gate, METRIC, gate.read(METRIC).replace("# Summary", "# Summary\n\nA hand note.", 1))
    owned = gate.post(gate.request(gate.put("metrics/owned.md", concept("Owned"))), token="owner")
    assert owned.status_code == 201

    served = backlog(gate)
    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH)  # a restart holds no backlog state to lose
    rebuilt = audit.backlog(gate.writer, auditors=gate.appmod._auditors(), now=datetime.now(UTC))

    assert sorted((entry["path"], entry["reason"]) for entry in served["concepts"]) == [
        ("metrics/owned.md", "generation"), (METRIC, "external")]
    assert rebuilt["concepts"] == served["concepts"] == backlog(gate)["concepts"]


def test_older_concepts_are_released_a_seed_a_day(gate) -> None:
    epoch = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # AIO_AB is older
    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=epoch, AIWIKI_AUDIT_SEED_PER_DAY="0")
    assert backlog(gate)["concepts"] == [] and backlog(gate)["seed"] == {
        "per_day": 0, "waiting": 1, "released": 0, "reviewed": 0}

    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=epoch, AIWIKI_AUDIT_SEED_PER_DAY="1")
    assert due(gate) == [(AIO_AB, "seed")]
    lease(gate)
    job = submit(gate, review(gate, AIO_AB, "unverified")).json()

    assert job["reviews"][0]["seed"] is True
    assert backlog(gate)["seed"] == {"per_day": 1, "waiting": 0, "released": 0, "reviewed": 1}


def test_a_push_past_the_service_puts_its_concepts_under_review(gate) -> None:
    """External attention (§5.3): a hand edit keeps ``generated`` and ``verified`` as they were,
    so its verified concept stays verification-current, yet it is due for review."""
    push(gate, METRIC, gate.read(METRIC).replace("# Summary", "# Summary\n\nA hand note.", 1))
    assert published(gate, METRIC)["verified"] and (METRIC, "external") in due(gate)

    lease(gate)
    assert submit(gate, review(gate, METRIC)).status_code == 201

    assert (METRIC, "external") not in due(gate)
    push(gate, METRIC, gate.read(METRIC).replace("A hand note.", "A second hand note."))
    assert (METRIC, "external") in due(gate)  # another push is another version to review
    # A generation after the push is a generation to review, due since the service stamped it.
    git(gate.writer, "merge", "-q", "--ff-only", "origin/main")  # the curator's base is the pushed version
    assert gate.post(gate.request()).status_code == 201
    entry = next(entry for entry in backlog(gate)["concepts"] if entry["path"] == METRIC)
    assert (entry["reason"], entry["since"]) == ("generation", published(gate, METRIC)["generated"]["at"])
    # Only a later service commit settles a push, never the stamps the push itself writes.
    forged = re.sub(r"(?m)^generated:.*\n(?:  .*\n)*", "generated: {by: process:ai-wiki-auditor, at: "
                    "'2099-01-01T00:00:00Z'}\nverified:\n- {by: process:ai-wiki-auditor, at: 2099-01-02T00:00:00Z}\n",
                    gate.read(METRIC).replace("A second hand note.", "Revenue doubled."), count=1)
    push(gate, METRIC, re.sub(r"(?m)^verified:\n(?:- .*\n)+(?=sources:)", "", forged, count=1))
    assert published(gate, METRIC)["generated"]["by"] == AUDITOR and len(published(gate, METRIC)["verified"]) == 1
    assert (METRIC, "external") in due(gate)


def test_external_attention_follows_ancestry_not_commit_dates(gate) -> None:
    """A commit dated before the epoch (a slow clock, a rebase) or merged in from an old branch
    is still after the epoch in history: nothing below or inside it escapes review."""
    old = "2026-09-19T12:00:00Z"  # before EPOCH

    def commit(repo, message: str, date: str | None = None) -> None:
        git(repo, "add", "-A")
        dated = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {}
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True, capture_output=True,
                       env={**os.environ, **dated})

    other = clone(gate.remote, gate.tmp / "other-dates")
    (other / METRIC).write_text(gate.read(METRIC).replace("# Summary", "# Summary\n\nA hand note.", 1),
                                encoding="utf-8")
    commit(other, "hand edit")
    (other / "notes.md").write_text("notes\n", encoding="utf-8")
    commit(other, "notes from a laptop whose clock is behind", old)
    git(other, "checkout", "-q", "-b", "topic", "HEAD~2")
    (other / AI_STUDY).write_text(gate.read(AI_STUDY) + "\nA branch note.\n", encoding="utf-8")
    commit(other, "branch edit", old)
    git(other, "checkout", "-q", "main")
    git(other, "merge", "-q", "--no-ff", "topic", "-m", "Merge topic")
    git(other, "push", "-q", "origin", "main")
    git(gate.writer, "fetch", "-q")

    assert {(METRIC, "external"), (AI_STUDY, "external")} <= set(due(gate))


def test_the_same_verdict_on_a_new_version_is_a_new_review(gate) -> None:
    """The idempotency key holds each review's base: a repeated verdict and note on a version
    pushed since is judged again, never deduplicated onto the old version's receipt."""
    lease(gate)
    sent = review(gate, AIO_AB, "unverified", note="lift not in S1")
    first = submit(gate, sent)
    assert first.status_code == 201 and submit(gate, sent).json()["id"] == first.json()["id"]  # a resend
    push(gate, AIO_AB, gate.read(AIO_AB).replace("Redacted fixture body.", "Redacted fixture body, edited."))
    assert (AIO_AB, "external") in due(gate)

    second = submit(gate, review(gate, AIO_AB, "unverified", note="lift not in S1"))

    assert second.json()["deduplicated"] is False and second.json()["id"] != first.json()["id"], second.text
    assert second.json()["reviews"][0]["base"] == changeset.content_hash(git(gate.remote, "show", f"main:{AIO_AB}"))
    assert (AIO_AB, "external") not in due(gate)


@pytest.mark.parametrize(("pushed", "verdict", "edit", "code"), [
    (lambda text: text.replace("# Summary", "# Summary\n\nConversion rose 40% in Q3.", 1), "unverified", None, None),
    (lambda text: text.replace("# Summary", "# Summary\n\nA hand note.", 1), "corrected",
     lambda text: text.replace("A hand note.", "A hand note. Lift was 12.5%."), "D_NOVEL_TOKEN"),
    (lambda text: re.sub(r"resource: /sources/\S+", "resource: https://example.com/elsewhere", text), "verified",
     None, "D_NO_EVIDENCE"),
])
def test_an_unverified_outcome_withdraws_the_verification_a_push_kept(gate, pushed, verdict, edit, code) -> None:
    """A push keeps ``generated`` and ``verified``, so the pushed version reads as verified. A
    review that does not verify it (by verdict or by downgrade) makes that verification
    historical: the service dates the generation at its own time, keeping its author."""
    before = published(gate, METRIC)
    push(gate, METRIC, pushed(gate.read(METRIC)))
    assert (METRIC, "external") in due(gate)
    lease(gate)
    extra = {"content": edit(git(gate.remote, "show", f"main:{METRIC}") + "\n")} if edit else {}

    job = submit(gate, review(gate, METRIC, verdict, **extra)).json()

    assert job["status"] == "done" and job["reviews"][0]["outcome"] == "unverified", job
    assert job["reviews"][0].get("downgrade") == code and job["commit"]
    after = published(gate, METRIC)
    assert after["verified"] == before["verified"] and after["generated"]["by"] == before["generated"]["by"]
    assert audit._instant(after["generated"]["at"]) > max(event["at"] for event in after["verified"])
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": METRIC},
                               headers=gate.headers("auditor")).json()["metadata"]
    assert (metadata["trust"], metadata["verification_current"]) == ("machine-confirmed", False)
    assert (METRIC, "external") not in due(gate)  # reviewed: new evidence brings a new version


def test_a_reverted_audit_returns_its_concept_to_the_backlog(gate) -> None:
    lease(gate)
    job = submit(gate, review(gate, AIO_AB)).json()
    assert due(gate) == []

    reverted = gate.client.post("/admin/revert", params={"bundle": "kb-a"}, headers=gate.headers("owner"),
                                json={"changeset": job["id"], "reason": "the auditor's token leaked"})

    assert reverted.status_code == 201, reverted.text
    assert due(gate) == [(AIO_AB, "generation")]


# --- the gate: A1–A8 (design §5.4) -------------------------------------------------------------------


def test_a_verified_review_is_stamped_by_the_service_and_leaves_the_backlog(gate) -> None:
    lease(gate)
    before = gate.read(AIO_AB)

    response = submit(gate, review(gate, AIO_AB))

    assert response.status_code == 201, response.text
    job = response.json()
    assert (job["kind"], job["mode"], job["status"], job["principal"], job["actor"]) == (
        "audit", "changeset", "done", AUDITOR, AUDITOR)
    assert job["audit"] == {"status": "passed", "verified_concepts": [AIO_AB], "unverified_concepts": [],
                            "corrected_concepts": []}
    assert job["concept_files"] == [AIO_AB] and job["validation"]["status"] == "passed"
    record = job["reviews"][0]
    assert (record["outcome"], record["base"]) == ("verified", changeset.content_hash(before))
    frontmatter = published(gate, AIO_AB)
    assert frontmatter["status"] == "stable" and frontmatter["generated"]["by"] == "process:ai-wiki-curator"
    event = frontmatter["verified"][-1]
    assert event["by"] == AUDITOR and job["started"] <= event["at"].strftime("%Y-%m-%dT%H:%M:%SZ") <= job["finished"]
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": AIO_AB},
                               headers=gate.headers("auditor")).json()["metadata"]
    assert (metadata["trust"], metadata["verification_current"]) == ("machine-confirmed", True)
    message = git(gate.remote, "log", "-1", "--format=%B", "main")
    assert f"Changeset: {job['id']}" in message and f"Principal: {AUDITOR}" in message and f"Run: {RUN}" in message
    assert due(gate) == []
    assert git(gate.writer, "status", "--porcelain") == ""


def test_a1_only_the_audit_scope_reviews(gate) -> None:
    head = gate.head()
    denied = submit(gate, review(gate, AIO_AB), token="curator")
    assert denied.status_code == 403 and "lacks scope" in denied.json()["detail"]
    with pytest.raises(auth.PrincipalsError, match="must not hold both curate and audit"):
        auth.parse({"principals": [{"id": "process:ai-wiki-auditor", "token_sha256": "0" * 64,
                                    "scopes": ["read", "audit", "curate"]}]})
    gate.assert_untouched(head)


def test_a2_an_auditor_reviews_only_the_backlog_and_a_human_anything(gate) -> None:
    lease(gate)
    head = gate.head()

    outside = submit(gate, review(gate, METRIC))
    missing = submit(gate, {**review(gate, METRIC), "path": "metrics/no-such-concept.md"})

    assert outside.status_code == 422 and outside.json()["errors"][0]["code"] == "audit_scope"
    assert "not in the audit backlog" in outside.json()["errors"][0]["message"]
    assert missing.status_code == 422 and missing.json()["errors"][0]["code"] == "audit_scope"
    gate.assert_untouched(head)
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", RUN))
    lease(gate, "owner")
    human = submit(gate, review(gate, METRIC), token="owner")
    assert human.status_code == 201, human.text
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": METRIC},
                               headers=gate.headers("owner")).json()["metadata"]
    assert metadata["trust"] == "human-reviewed" and published(gate, METRIC)["verified"][-1]["by"] == "human:owner"


def test_a3_a_review_of_another_version_is_a_conflict(gate) -> None:
    lease(gate)
    stale = review(gate, AIO_AB)
    push(gate, AIO_AB, gate.read(AIO_AB).replace("Redacted fixture body.", "Redacted fixture body, edited."))
    head = gate.remote_head()

    response = submit(gate, stale)

    assert response.status_code == 409, response.text
    assert response.json()["conflicts"][0]["path"] == AIO_AB
    assert gate.remote_head() == head


def test_a4_no_reviewer_verifies_its_own_generation(gate) -> None:
    owned = gate.post(gate.request(gate.put("metrics/owned.md", concept("Owned"))), token="owner")
    assert owned.status_code == 201 and ("metrics/owned.md", "generation") in due(gate)
    lease(gate, "owner")

    own = submit(gate, review(gate, "metrics/owned.md"), token="owner")

    assert own.status_code == 422 and own.json()["errors"][0]["code"] == "self_verification"
    # By class too: one auditor never verifies another auditor's correction.
    second = "process:ai-wiki-auditor-2"
    original = gate.read(AIO_AB)
    (gate.writer / AIO_AB).write_text(original.replace("'process:ai-wiki-curator'", AUDITOR), encoding="utf-8")
    request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": gate.head(),
               "reviews": [review(gate, AIO_AB)]}
    judged = audit.evaluate_review(gate.writer, request, actor=second, now=datetime.now(UTC),
                                   scope={AIO_AB: {"reason": "generation"}},
                                   auditors=audit.AUDITOR_ACTORS | {second})
    pushed = audit.evaluate_review(gate.writer, request, actor=second, now=datetime.now(UTC),
                                   scope={AIO_AB: {"reason": "external"}}, auditors=audit.AUDITOR_ACTORS | {second})
    # The author's own review of a push its generation still names counts only SELF_REVIEW_DELAY
    # past that generation, as current_verified reads it; until then the gate refuses it.
    at = audit._instant(parse_document(gate.read(AIO_AB)).frontmatter["generated"]["at"])
    early, late = (audit.evaluate_review(gate.writer, request, actor=AUDITOR, now=at + timedelta(hours=hours),
                                         scope={AIO_AB: {"reason": "external"}}, auditors=audit.AUDITOR_ACTORS)
                   for hours in (11, 13))
    (gate.writer / AIO_AB).write_text(original, encoding="utf-8")
    assert judged["errors"][0]["code"] == "self_verification"
    assert pushed["status"] == "would_apply"  # a push since then is someone else's version to review
    assert early["errors"][0]["code"] == "self_verification" and late["status"] == "would_apply", (early, late)


def test_a4_a_correction_is_reviewed_by_a_later_run_never_its_own(gate, monkeypatch) -> None:
    """A correction is the auditor's new generation, unverified (rule 2). It returns to the
    backlog as a ``correction`` (rule 3): neither the run that wrote it nor, before
    SELF_REVIEW_DELAY has passed by the writer's clock, any other run label of the auditor (the
    same session renamed) may judge it; a later session may, and only its verified or
    unverified judgement settles it. The read side counts that later event, not an earlier one."""
    lease(gate)
    job = submit(gate, review(gate, AIO_AB, "corrected", content=narrowed(gate.read(AIO_AB)))).json()
    record = job["reviews"][0]
    assert (record["outcome"], record["run"]) == ("corrected", RUN) and job["audit"]["status"] == "needs_attention"
    assert job["audit"]["verified_concepts"] == [] and job["audit"]["corrected_concepts"] == [AIO_AB]
    after = published(gate, AIO_AB)
    assert after["generated"]["by"] == AUDITOR and after["verified"] == parse_document(
        gate.read(AIO_AB)).frontmatter["verified"]  # history kept, no event of its own
    entry = next(entry for entry in backlog(gate)["concepts"] if entry["path"] == AIO_AB)
    assert (entry["reason"], entry["corrected_run"], entry["base"], entry["legacy"]) == (
        "correction", RUN, record["content_hash"], False)
    assert entry["since"] == after["generated"]["at"] and entry["verification_current"] is False
    assert audit._instant(entry["reviewable_at"]) == audit._instant(job["finished"]) + audit.SELF_REVIEW_DELAY

    again = submit(gate, review(gate, AIO_AB))  # the same run: its own edit
    assert again.status_code == 422 and again.json()["errors"][0]["code"] == "self_verification", again.text
    assert "in the run that wrote it" in again.json()["errors"][0]["message"]
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", RUN))
    lease(gate, run="AUD-2")
    renamed = submit(gate, review(gate, AIO_AB), run="AUD-2")  # a new label in the same session
    assert renamed.status_code == 422 and renamed.json()["errors"][0]["code"] == "self_verification", renamed.text
    assert f"before {entry['reviewable_at']}, 12 h after its generation" in renamed.json()["errors"][0]["message"]

    later(monkeypatch, 13)
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", "AUD-2"))
    lease(gate)
    assert submit(gate, review(gate, AIO_AB)).json()["errors"][0]["code"] == "self_verification"  # still its own
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", RUN))
    lease(gate, run="AUD-2")
    next_day = submit(gate, review(gate, AIO_AB), run="AUD-2")
    assert next_day.status_code == 201 and next_day.json()["reviews"][0]["outcome"] == "verified", next_day.text
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": AIO_AB},
                               headers=gate.headers("auditor")).json()["metadata"]
    assert metadata["verification_current"] is True and metadata["current_verified_by"] == [AUDITOR]
    assert (AIO_AB, "correction") not in due(gate)


def test_a_correction_back_to_a_reviewed_version_is_due_again_and_names_the_newest_run(gate, monkeypatch) -> None:
    """Rule 5: an older judgement of the same bytes does not settle what a correction wrote
    since. AUD-1 verifies AIO_AB; a push adds a claim; AUD-2 deletes exactly that claim, so its
    correction leaves the version AUD-1 verified, now the auditor's unverified generation. It
    is due again, and its correcting run is AUD-2's (the newest), whatever order the receipts
    are read in; only a review newer than the correction settles it."""
    lease(gate)
    assert submit(gate, review(gate, AIO_AB)).json()["reviews"][0]["outcome"] == "verified"
    judged = changeset.content_hash(gate.read(AIO_AB))
    push(gate, AIO_AB, gate.read(AIO_AB).replace("Redacted fixture body.", "Redacted fixture body. It won by 9%."))
    assert (AIO_AB, "external") in due(gate)
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", RUN))
    lease(gate, run="AUD-2")
    pushed = git(gate.remote, "show", f"main:{AIO_AB}") + "\n"
    job = submit(gate, review(gate, AIO_AB, "corrected", content=pushed.replace(" It won by 9%.", "")),
                 run="AUD-2").json()
    assert job["reviews"][0]["outcome"] == "corrected" and job["reviews"][0]["content_hash"] == judged, job

    entry = next(entry for entry in backlog(gate)["concepts"] if entry["path"] == AIO_AB)
    assert (entry["reason"], entry["base"], entry["corrected_run"], entry["legacy"]) == (
        "correction", judged, "AUD-2", False)
    later(monkeypatch, 13)
    gate.client.delete("/maint/lease/auditor", params={"bundle": "kb-a"}, headers=gate.headers("auditor", "AUD-2"))
    lease(gate, run="AUD-3")
    assert submit(gate, review(gate, AIO_AB, "unverified"), run="AUD-3").json()["reviews"][0]["outcome"] == \
        "unverified"
    assert AIO_AB not in [path for path, _reason in due(gate)]  # a judgement newer than the correction


def test_the_newest_correction_names_the_run_whatever_order_receipts_are_read(tmp_path, monkeypatch) -> None:
    """Two runs wrote the same bytes: the newest receipt by finish names the correcting run and
    time (A4); job ids are random, so the order the job files are listed in must not decide."""
    jobs = tmp_path / ".okf" / "jobs"
    jobs.mkdir(parents=True)

    def receipt(job_id: str, run: str, finished: str, **review) -> None:
        (jobs / f"{job_id}.json").write_text(json.dumps({
            "id": job_id, "kind": "audit", "mode": "changeset", "status": "done", "run": run, "started": finished,
            "finished": finished, "reviews": [{"path": "m.md", "base": "b0", "content_hash": "c1",
                                               "verdict": "corrected", "outcome": "corrected", "run": run,
                                               **review}]}), encoding="utf-8")

    receipt("0000", "AUD-2", "2026-10-17T02:00:00Z")
    receipt("ffff", "AUD-1", "2026-10-16T02:00:00Z")
    receipt("8888", "AUD-0", "2026-10-15T02:00:00Z", base="b9", verdict="verified", outcome="verified",
            content_hash="c1")  # an older judgement of the same bytes
    listed = I.changeset_jobs(tmp_path)
    for order in (listed, listed[::-1]):
        monkeypatch.setattr(I, "changeset_jobs", lambda _bundle, order=order: list(order))
        reviewed, _seeded, corrected = audit.review_records(tmp_path)
        assert corrected[("m.md", "c1")] == {"run": "AUD-2", "at": audit._instant("2026-10-17T02:00:00Z"),
                                             "order": 2, "legacy": False}
        assert reviewed[("m.md", "c1")] < corrected[("m.md", "c1")]["order"]  # it settles nothing


def test_legacy_corrections_wait_behind_new_work_and_never_page(gate) -> None:
    """Legacy corrections (rule 4): an auditor's correction that stamped itself verified before
    A4 (rule 1 reads it unverified), or a Codex audit's left unverified, has no receipt written
    under A4's rule. It is due as a ``legacy`` ``correction`` that any auditor run may review,
    after every new generation, external change and receipted correction but before seeds, and
    /maint/status leaves it out of the age the watchdog pages on, as it does seeds. A receipted
    correction is new work, due from its correction."""
    epoch = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # every fixture is older
    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=epoch, AIWIKI_AUDIT_SEED_PER_DAY="1")
    stamp = "2026-10-08T23:14:12Z"
    selfish = re.sub(r"(?m)^generated:\n(?:  .*\n)+", f"generated:\n  by: {AUDITOR}\n  at: '{stamp}'\n",
                     gate.read(METRIC), count=1)
    selfish = selfish.replace("verified:\n", f"verified:\n- {{by: {AUDITOR}, at: {stamp}}}\n", 1)
    push(gate, METRIC, selfish, "Changeset: legacy-review\n\nreview: 1 concept(s)")
    assert published(gate, METRIC)["verified"][0]["by"] == AUDITOR
    assert due(gate) == [(METRIC, "correction"), (AIO_AB, "seed")]
    lease(gate)
    assert submit(gate, review(gate, AIO_AB, "corrected", content=narrowed(gate.read(AIO_AB)))).status_code == 201
    owned = gate.post(gate.request(gate.put("metrics/owned.md", concept("Owned"))), token="owner")
    assert owned.status_code == 201

    found = backlog(gate)["concepts"]
    assert [(entry["path"], entry["reason"], entry.get("legacy")) for entry in found] == [
        (AIO_AB, "correction", False), ("metrics/owned.md", "generation", None), (METRIC, "correction", True)]
    legacy = found[-1]
    assert (legacy["corrected_run"], legacy["verification_current"], legacy["since"]) == (None, False, stamp)
    assert legacy["reviewable_at"] == "2026-10-09T11:14:12Z"  # long past: any auditor run reviews it
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers("auditor")).json()
    assert (status["audit"]["pending"], status["audit"]["oldest_finished"]) == (3, found[0]["since"])
    assert submit(gate, review(gate, METRIC)).json()["reviews"][0]["outcome"] == "verified"
    assert METRIC not in [path for path, _reason in due(gate)]


def test_a_human_correction_is_verified_by_that_human(gate) -> None:
    """Rule 2 binds process reviewers only: a person's ``corrected`` verdict is the new
    generation and its verification in one act (§5.5), human-reviewed and current, and leaves
    the backlog; A4's delay is not theirs either."""
    lease(gate, "owner")
    original = submit(gate, review(gate, METRIC, "corrected", content=narrowed(gate.read(METRIC))), token="owner")
    assert original.status_code == 201, original.text
    job = original.json()
    assert (job["reviews"][0]["outcome"], job["reviews"][0]["verdict"]) == ("corrected", "corrected")
    assert job["audit"] == {"status": "passed", "verified_concepts": [METRIC], "unverified_concepts": [],
                            "corrected_concepts": [METRIC]}
    after = published(gate, METRIC)
    assert after["generated"]["by"] == after["verified"][-1]["by"] == "human:owner"
    assert audit._instant(after["generated"]["at"]) == audit._instant(after["verified"][-1]["at"])
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": METRIC},
                               headers=gate.headers("owner")).json()["metadata"]
    assert (metadata["trust"], metadata["verification_current"]) == ("human-reviewed", True)
    assert METRIC not in [path for path, _reason in due(gate)]


def test_a5_sources_identity_and_freshness_are_restored_not_refused(gate) -> None:
    lease(gate)
    before = gate.read(AIO_AB)
    frontmatter = parse_document(before).frontmatter
    first = frontmatter["sources"][0]["resource"]
    edited = narrowed(before).replace(f"    resource: {first}\n", "    resource: https://example.com/elsewhere\n")
    edited = edited.replace("type: Experiment", "type: Decision")
    edited = edited.replace("title: Redacted title\n", "title: Other\n", 1)
    edited = edited.replace("confidence: medium\n", "confidence: medium\nstale_after: 2099-01-01\n")

    job = submit(gate, review(gate, AIO_AB, "corrected", content=edited)).json()

    assert job["status"] == "done" and job["reviews"][0]["outcome"] == "corrected", job
    assert set(job["deterministic_repairs"][AIO_AB]) >= {
        "restored immutable sources provenance", "restored identity-locked type",
        "restored identity-locked title", "restored frozen stale_after"}
    after = published(gate, AIO_AB)
    assert after["sources"] == frontmatter["sources"] and (after["type"], after["title"]) == ("Experiment",
                                                                                             "Redacted title")
    assert "stale_after" not in after
    # A correction is the auditor's own generation, which it never verifies (A4): no event added.
    assert after["generated"]["by"] == AUDITOR and after["verified"] == frontmatter["verified"]
    assert job["audit"] == {"status": "needs_attention", "verified_concepts": [], "unverified_concepts": [AIO_AB],
                            "corrected_concepts": [AIO_AB]}
    assert "Redacted body." in git(gate.remote, "show", f"main:{AIO_AB}")


@pytest.mark.parametrize(("edit", "code"), [
    (lambda text: narrowed(text).replace("Redacted body.", "Redacted body. Lift was 12.5%."), "D_NOVEL_TOKEN"),
    (lambda text: narrowed(text).replace("Redacted body.", "Redacted body with `checkout_retry_rate`."),
     "D_NOVEL_TOKEN"),
    (lambda text: text.replace("Redacted fixture body.", "Redacted fixture body, which the redacted "
                                                         "fixture body restates."), "D_GROWTH"),
    (lambda text: text.replace("Redacted fixture body.", "[R](x.md) fixture body."), "D_NEW_LINK"),
    (lambda text: text.replace("status: draft\n", f"status: draft\ncontested: true\ncontradictions: [{AI_STUDY}]\n"),
     "D_NEW_LINK"),
    (lambda text: text.replace("description: Redacted description", "description: Redacted description. The "
                                                                   "variant won and was released worldwide"),
     "D_GROWTH"),
    (lambda text: text.replace("confidence: medium", "confidence: high"), "D_GROWTH"),
])
def test_a6_a_correction_that_adds_is_downgraded_not_failed(gate, edit, code) -> None:
    lease(gate)
    before = gate.read(AIO_AB)

    response = submit(gate, review(gate, AIO_AB, "corrected", content=edit(before)))

    assert response.status_code == 201, response.text
    job = response.json()
    assert job["reviews"][0] | {"content_hash": None, "note": None} == {
        "path": AIO_AB, "base": changeset.content_hash(before), "verdict": "corrected", "outcome": "unverified",
        "downgrade": code, "content_hash": None, "note": None, "run": RUN}
    assert job["audit"]["status"] == "needs_attention" and job["audit"]["unverified_concepts"] == [AIO_AB]
    after = git(gate.remote, "show", f"main:{AIO_AB}") + "\n"
    assert parse_document(after).body == parse_document(before).body.replace(ORPHAN + "\n", "")  # HEAD kept
    assert published(gate, AIO_AB)["status"] == "stable"
    assert published(gate, AIO_AB)["verified"] == parse_document(before).frontmatter["verified"]  # no new event
    assert due(gate) == []  # reviewed at this content: the maintainer brings new evidence first


@pytest.mark.parametrize("edit", [
    lambda text: text.replace("title: Redacted title\n", "title: Redacted\n", 1),
    lambda text: re.sub(r"resource: /sources/\S+", "resource: https://example.com/elsewhere", text, count=1),
    lambda text: text.replace("confidence: medium\n", "confidence: medium\nstale_after: 2026-10-01\n"),
])
def test_a5_a_correction_it_restores_entirely_is_no_verification(gate, edit) -> None:
    """The reviewer judged the concept wrong where a review may not change it: nothing of the
    correction lands, so the concept ends unverified, never verified as it stood."""
    lease(gate)
    before = gate.read(AIO_AB)

    job = submit(gate, review(gate, AIO_AB, "corrected", content=edit(before), note="the title overstates")).json()

    assert job["status"] == "done", job
    assert (job["reviews"][0]["outcome"], job["reviews"][0]["downgrade"]) == ("unverified", "D_RESTORED")
    after = published(gate, AIO_AB)
    assert (after["title"], after["sources"]) == (parse_document(before).frontmatter["title"],
                                                  parse_document(before).frontmatter["sources"])
    assert "stale_after" not in after and after["verified"] == parse_document(before).frontmatter["verified"]


def test_a6_whole_words_decide_what_a_correction_adds() -> None:
    before = "---\ntype: Metric\ntitle: T\n---\n# Summary\n\nRelease v2 shipped 5k seats on 2026-09-17T10:00Z.\n"
    known = before + "\nsha256 a55e99db8c\n"

    assert audit._narrowing(before, before.replace("shipped", "may have shipped"), known) is None
    assert audit._narrowing(before, before.replace("5k", "99"), known) == "D_NOVEL_TOKEN"  # 99 is only in a hash
    assert audit._narrowing(before, before.replace("v2", "v3"), known) is None  # not a number word
    assert audit._narrowing(before, before.replace("seats", "`seat_count`"), known) == "D_NOVEL_TOKEN"


def test_a6_content_keys_narrow_like_the_body() -> None:
    before = ("---\ntype: Metric\ntitle: T\ndescription: Released to all users and won\ntags: [a, b]\n"
              "confidence: medium\ncontradictions: [x.md]\ncontested: true\n---\n# Summary\n\nBody.\n")

    narrowed = before.replace("Released to all users and won", "Merged; release unconfirmed").replace(
        "[a, b]", "[a]").replace("medium", "low")
    assert audit._narrowing(before, narrowed, before) is None
    for edit in ("tags: [a, b, c]", "confidence: high", "aliases: [a]", "contested: false",
                 "description: Released to all users and won, lifting every market's paid conversion"):
        key = edit.split(":")[0]
        widened = re.sub(rf"(?m)^{key}:.*$", edit, before) if f"\n{key}:" in before else before.replace(
            "---\n#", edit + "\n---\n#")
        assert audit._narrowing(before, widened, before) == "D_GROWTH", edit
    assert audit._narrowing(before, before.replace("[x.md]", "[]"), before) == "D_GROWTH"  # a caveat cleared
    # Removing an optional assertive key says less (the 2026-09-29 downgrades deleted `owner`).
    owned = before.replace("confidence: medium\n", "confidence: medium\nowner: growth\naliases: [x]\n")
    for key in ("owner", "aliases"):
        assert audit._narrowing(owned, re.sub(rf"(?m)^{key}:.*\n", "", owned), owned) is None, key
    # A hedge must stay: without a caveat or a confidence below high the concept reads unhedged.
    # A required key must stay too, or validation would refuse the auditor's whole changeset.
    for key in ("confidence", "contested", "contradictions", "tags", "description"):
        assert audit._narrowing(owned, re.sub(rf"(?m)^{key}:.*\n", "", owned), owned) == "D_GROWTH", key
    sure = owned.replace("confidence: medium", "confidence: high")
    assert audit._narrowing(sure, sure.replace("confidence: high\n", ""), sure) is None
    assert audit._narrowing(before, before.replace("[x.md]", "[x.md, y.md]"), before) == "D_NEW_LINK"


def test_a6_a_correction_keeps_every_caveat() -> None:
    """D_CAVEAT_REMOVED: d0e2a12 deleted the "快照非结论" guard and stamped the rest verified.
    A hedge may be reworded or moved, and goes with a claim deleted whole; dropping it, or its
    hedge word alone, while the claim it bounds stays, widens the concept."""
    before = ("---\ntype: Experiment\ntitle: T\ndescription: 2026-08-21 首读数仅为方向性快照\n---\n# Readout\n\n"
              "Humanizer 耗时 +27.3%，p<0.001。\n\n"
              "- **快照非结论：** 相关数值是当时的分析快照，不构成实验赢家。Cramér's V 约 0.57。\n"
              "- 合并只证明代码进入 `main`，不证明已部署。\n"
              "- The lift was 4.2% in a preliminary readout of `TEST_Web_X`.\n"
              "- Only desktop users were sampled in `TEST_Web_X`.\n")
    removed = before.replace("- **快照非结论：** 相关数值是当时的分析快照，不构成实验赢家。Cramér's V 约 0.57。\n",
                             "- Cramér's V 约 0.57。\n")
    assert audit._narrowing(before, removed, before) == "D_CAVEAT_REMOVED"
    for edit in (before.replace("，不证明已部署", ""),  # the clause alone
                 before.replace("不证明已部署", "已部署"),  # inverted: the hedge word alone
                 before.replace(" in a preliminary readout", ""),
                 before.replace("description: 2026-08-21 首读数仅为方向性快照", "description: 2026-08-21 首读数")):
        assert audit._narrowing(before, edit, before) == "D_CAVEAT_REMOVED", edit
    merged = "合并只证明代码进入 `main`，不证明已部署。\n"
    for edit in (before.replace("不构成实验赢家", "仍不足以判定实验赢家"),  # reworded with another limit
                 before.replace("- " + merged, "") + "\n" + merged,  # moved
                 before.replace("- The lift was 4.2% in a preliminary readout of `TEST_Web_X`.\n", ""),  # claim too
                 before.replace("- Only desktop users were sampled in `TEST_Web_X`.\n", ""),  # "only" scopes
                 before.replace("Humanizer 耗时 +27.3%，p<0.001。\n\n", "")):
        assert audit._narrowing(before, edit, before) is None, edit
    # A hedge that states no claim bounds the whole concept; one moved into a footnote no longer bounds it.
    bare = before.replace("- **快照非结论：** 相关数值是当时的分析快照，不构成实验赢家。Cramér's V 约 0.57。\n",
                          "- 读数窗口未完整，不构成实验赢家。\n")
    assert audit._narrowing(bare, bare.replace("- 读数窗口未完整，不构成实验赢家。\n", ""), bare) == "D_CAVEAT_REMOVED"
    noted = before.replace("，不证明已部署。", "。[^m]") + "\n[^m]: 不证明已部署。\n"
    for edit in (noted.replace("[^m]: 不证明已部署。", "[^m]: the merge log."),  # the definition reworded
                 noted.replace("。[^m]", "。"),  # uncited: a definition no line cites documents nothing
                 noted.replace("。[^m]", "。").replace("\n[^m]: 不证明已部署。\n", "\n")):  # deleted with its marker
        assert audit._narrowing(noted, edit, noted) == "D_CAVEAT_REMOVED", edit
    for edit in (noted.replace("- 合并只证明代码进入 `main`。[^m]\n", "").replace("\n[^m]: 不证明已部署。\n", "\n"),
                 noted.replace("[^m]", "[^merge]")):  # deleted with what cites it; relabelled
        assert audit._narrowing(noted, edit, noted) is None, edit
    orphan = before + "\n[^x]: 早期快照，不构成赢家依据。\n"  # HEAD itself cites it nowhere
    assert audit._narrowing(orphan, before, orphan) is None


@pytest.mark.parametrize(("head", "edit", "code"), [
    # A guard naming only a date or an issue id still bounds the readout it is about.
    ("2026-08-21 的读数是早期快照，不构成赢家依据。\n\n- Humanizer 耗时 +27.3%。\n", "- Humanizer 耗时 +27.3%。\n",
     "D_CAVEAT_REMOVED"),
    ("WAIO-158 的读数是早期快照，不构成赢家依据。\n\n- Humanizer 耗时 +27.3%。\n", "- Humanizer 耗时 +27.3%。\n",
     "D_CAVEAT_REMOVED"),
    ("- 下述 2026-09-24 结果只是方向性观察。\n- 2026-09-24 读数：T 197，C 58。\n", "- 2026-09-24 读数：T 197，C 58。\n",
     "D_CAVEAT_REMOVED"),
    ("- 留存 +9.9%。\n- 2026-08-21 初步读数：耗时 +27.3%。\n", "- 留存 +9.9%。\n", None),  # the claim with it
    # A polarity flip is no rewording, whatever limit or filler it adds.
    ("- 合并只证明代码进入 `main`，不证明 `gray` 已部署。\n",
     "- 合并只证明代码进入 `main`，证明 `gray` 已部署，没有例外。\n", "D_CAVEAT_REMOVED"),
    ("- 读数 +4.1%，不构成实验赢家。\n", "- 读数 +4.1%，构成实验赢家，可能。\n", "D_CAVEAT_REMOVED"),
    ("- The lift of 4.2% is not conclusive.\n", "- The lift of 4.2% is conclusive, only.\n", "D_CAVEAT_REMOVED"),
    ("- 不能证明 `gray` 已部署。\n", "- 没有证据表明 `gray` 已部署。\n", None),  # another strong limit
    # A hedged heading bounds its section, unless it is the concept's own bookkeeping.
    ("# 未验证项\n\n- 留存 +9.9%。\n", "# 验证项\n\n- 留存 +9.9%。\n", "D_CAVEAT_REMOVED"),
    ("# 优化方向（待验证）\n\n- 把 `cta_v2` 放首屏。\n", "# 优化方向\n\n- 把 `cta_v2` 放首屏。\n", "D_CAVEAT_REMOVED"),
    ("# Readout\n\n- 耗时 +27.3%。\n\n# 未验证项\n\n- 留存 +9.9%。\n", "# Readout\n\n- 耗时 +27.3%。\n", None),
    ("# 未验证项\n\n- 留存 +9.9%。\n", "## 未验证项\n\n- 留存 +9.9%。\n", None),
    ("# 尚未 `verified`\n\n- 留存 +9.9%。\n", "# 说明\n\n- 留存 +9.9%。\n", None),
])
def test_a6_dated_guards_flips_and_headings_keep_their_caveat(head, edit, code) -> None:
    front = "---\ntype: Experiment\ntitle: T\n---\n"
    assert audit._narrowing(front + head, front + edit, front + head) == code


def test_a6_the_caveat_check_stays_fast_on_a_large_concept() -> None:
    """The edit's units are indexed by word pair and word, so one review of a concept near the
    128 KB limit stays well under a second; it used to compare every hedge with every unit
    (79 s for 3000 hedged lines narrowed to identical ones)."""
    front = "---\ntype: Metric\ntitle: T\n---\n# T\n\n"
    hedged = "".join(f"- 初步读数 metric_a{n} 为 {n}.5%。\n" for n in range(3000))
    for edit, removed in (("- 读数为。\n" * 3000, False),  # each claim deleted with its hedge: a narrowing
                          ("".join(f"- 读数 metric_a{n} 为 {n}.5%。\n" for n in range(3000)), True)):
        started = time.perf_counter()
        found = audit._caveat_removed(parse_document(front + hedged), parse_document(front + edit), ())
        assert (found is not None) is removed
        assert time.perf_counter() - started < 5  # about 0.4 s; the margin is for a loaded CI host


def test_a6_d0e2a12_replayed_is_downgraded(gate) -> None:
    """The production correction d0e2a12 (experiments/web-undetectable-provider-experiment.md),
    replayed on a concept holding its guard section: deleting the guard keeps the concept as it was."""
    guard = ("# 2026-08-21 读数护栏（WAIO-158）\n\n这些是**读数纪律约束**，不是可推广的效果结论。\n\n"
             "- **读数窗口未完整、且存在强混杂：** Cramér's V 约 **0.57**；不应宣布 treatment 胜出。\n"
             "- **快照非结论：** 相关数值是当时的分析快照，不是可推广的效果结论，也不构成实验赢家。\n\n"
             "## 首轮读数数值\n\n仓库报告给出了上述护栏背后的数值：Humanizer 耗时 **+27.3%**，Cramér's V=0.581。\n")
    original = gate.read(AIO_AB)
    (gate.writer / AIO_AB).write_text(original.replace("Redacted fixture body.", guard), encoding="utf-8")
    head = gate.read(AIO_AB)
    deleted = re.sub(r"# 2026-08-21 读数护栏.*?(?=## 首轮读数数值)", "", head, flags=re.DOTALL).replace(
        "给出了上述护栏背后的数值", "给出了首轮读数")
    request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": gate.head(), "run": RUN, "reviews": [
        {"path": AIO_AB, "base": changeset.content_hash(head), "verdict": "corrected", "content": deleted,
         "note": "the guard is unsupported"}]}

    judged = audit.evaluate_review(gate.writer, request, actor=AUDITOR, now=datetime.now(UTC),
                                   scope={AIO_AB: {"reason": "generation"}}, auditors=audit.AUDITOR_ACTORS)
    (gate.writer / AIO_AB).write_text(original, encoding="utf-8")

    assert judged["status"] == "would_apply", judged
    assert (judged["reviews"][0]["outcome"], judged["reviews"][0]["downgrade"]) == ("unverified", "D_CAVEAT_REMOVED")
    assert "快照非结论" in judged["files"][AIO_AB] and judged["audit"]["unverified_concepts"] == [AIO_AB]


def test_a7_a_deprecated_concept_is_never_reviewed(gate) -> None:
    retired = gate.post(gate.request(gate.deprecate(AI_STUDY)), token="owner")
    assert retired.status_code == 201 and published(gate, AI_STUDY)["status"] == "deprecated"
    lease(gate, "owner")

    response = submit(gate, review(gate, AI_STUDY), token="owner")

    assert response.status_code == 422 and "deprecated" in response.json()["errors"][0]["message"]
    assert published(gate, AI_STUDY)["status"] == "deprecated"


@pytest.mark.parametrize("verdict", [None, "Verified", 42, "corrected"])
def test_a8_a_missing_or_unusable_verdict_concludes_unverified(gate, verdict) -> None:
    lease(gate)
    body = review(gate, AIO_AB, verdict)
    if verdict is None:
        del body["verdict"]

    job = submit(gate, body).json()

    assert job["status"] == "done", job
    assert (job["reviews"][0]["outcome"], job["reviews"][0]["downgrade"]) == ("unverified", "D_INVALID_VERDICT")
    assert published(gate, AIO_AB)["status"] == "stable" and job["audit"]["status"] == "needs_attention"


def test_a_note_is_never_evidence(gate) -> None:
    """A concept citing no frozen source cannot be verified, whatever the reviewer writes."""
    text = concept("Hearsay").replace("sources:\n- {id: funnel-status-2026-09-24, resource: evidence:packet}\n",
                                      "sources:\n- {id: hearsay, resource: 'https://example.com/hearsay'}\n")
    text = text.replace("tags: [metric]\n", "tags: [metric]\nstatus: draft\ngenerated: {by: "
                        "'process:ai-wiki-maintainer', at: '2026-09-27T00:00:00Z'}\n").replace(
        "[^funnel-status-2026-09-24]", "[^hearsay]")
    push(gate, "metrics/hearsay.md", text)
    assert ("metrics/hearsay.md", "external") in due(gate)
    lease(gate)

    job = submit(gate, review(gate, "metrics/hearsay.md", note="I checked the vendor dashboard myself")).json()

    assert (job["reviews"][0]["outcome"], job["reviews"][0]["downgrade"]) == ("unverified", "D_NO_EVIDENCE")
    assert "verified" not in published(gate, "metrics/hearsay.md")


@pytest.mark.parametrize("source", ["unrecorded", "drifted"])
def test_only_sources_the_ledger_froze_are_evidence(gate, source) -> None:
    """A file a push added, or a cited source a push rewrote, is not in sources/.hashes.yaml as
    it stands: it is no frozen evidence, so it cannot carry a verification. A push that only
    rewrites the evidence puts the concepts citing it under review too."""
    cited = parse_document(gate.read(METRIC)).frontmatter["sources"][0]["resource"].lstrip("/")
    other = clone(gate.remote, gate.tmp / "other-evidence")
    if source == "unrecorded":
        text = gate.read(METRIC).replace(f"resource: /{cited}", "resource: /sources/fake-note.md")
        cited = "sources/fake-note.md"
        (other / METRIC).write_text(text, encoding="utf-8")
    (other / cited).write_text("The funnel moved. (agent-written note)\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "hand edit")
    git(other, "push", "-q", "origin", "main")
    git(gate.writer, "fetch", "-q")
    entry = next(entry for entry in backlog(gate)["concepts"] if entry["path"] == METRIC)
    assert entry["reason"] == "external" and entry["sources"] == []
    lease(gate)

    job = submit(gate, review(gate, METRIC)).json()

    assert (job["reviews"][0]["outcome"], job["reviews"][0]["downgrade"]) == ("unverified", "D_NO_EVIDENCE"), job
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": METRIC},
                               headers=gate.headers("auditor")).json()["metadata"]
    assert metadata["verification_current"] is False


# --- replay of the real Codex audits 71ea85ca9c20 / e94c8b707aea -------------------------------------


def _lifted(text: str) -> str:
    """The reviewer's output in both failed audits: the 2026-09-19 orphan lifted into ``verified``
    with its own new event, ``generated`` refreshed and the concept promoted to stable."""
    tail = f"  - {{by: {audit.AUDITOR}, at: 2026-09-17T21:18:46Z}}\n"
    lifted = text.replace(tail + "---\n" + ORPHAN + "\n", tail + ORPHAN + "\n"
                          + f"  - {{by: {audit.AUDITOR}, at: 2026-09-23T15:52:00Z}}\n---\n")
    lifted = lifted.replace("  by: 'process:ai-wiki-curator'\n  at: '2026-09-23T15:46:00Z'",
                            f"  by: {audit.AUDITOR}\n  at: '2026-09-23T15:52:00Z'")
    assert lifted != text
    return lifted.replace("status: draft", "status: stable")


@pytest.mark.parametrize("verdict", ["verified", "corrected"])
def test_replay_of_71ea85ca9c20_and_e94c8b707aea_as_audit_changesets(gate, verdict) -> None:
    """Both audits of ingest 6f4b6f97e4b2 failed ("audit verification timestamp is outside the
    trusted audit window") because the reviewer wrote bookkeeping. As an audit changeset the
    same verdict, with or without that output as its correction, lands: the service keeps the
    history, removes the orphan from the body and adds one event at its own time."""
    original = gate.read(AIO_AB)
    assert ("---\n" + ORPHAN + "\n# Summary") in original
    lease(gate)
    extra = {"content": _lifted(original)} if verdict == "corrected" else {}

    response = submit(gate, review(gate, AIO_AB, verdict, **extra))

    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "done" and job["validation"]["status"] == "passed"
    assert job["reviews"][0]["outcome"] == "verified"  # lifting bookkeeping is no correction
    assert f"removed spilled frontmatter line from body: {ORPHAN.strip()!r}" in job["deterministic_repairs"][AIO_AB]
    after = git(gate.remote, "show", f"main:{AIO_AB}") + "\n"
    events = parse_document(after).frontmatter["verified"]
    assert events[:-1] == parse_document(original).frontmatter["verified"] and events[-1]["by"] == AUDITOR
    assert parse_document(after).frontmatter["generated"] == parse_document(original).frontmatter["generated"]
    assert body_spill_errors(parse_document(after).body) == [] and ORPHAN not in after
    expected = original.replace("status: draft", "status: stable").replace("---\n" + ORPHAN + "\n", "---\n")
    assert re.sub(r"\n  - \{by: process:ai-wiki-auditor, at: [^}]+\}\n---", "\n---", after, count=1) == expected


# --- modes, leases and the legacy route ------------------------------------------------------------


def test_codex_mode_judges_reviews_but_never_commits_them(gate) -> None:
    gate.app(AIWIKI_AUDIT="codex")
    lease(gate)
    head = gate.head()

    judged = submit(gate, review(gate, AIO_AB), dry_run=True)
    committed = submit(gate, review(gate, AIO_AB))

    assert judged.status_code == 200 and judged.json()["status"] == "would_apply", judged.text
    assert judged.json()["dry_run"] is True and AIO_AB in judged.json()["diffs"]
    assert committed.status_code == 403 and "AIWIKI_AUDIT=external" in committed.json()["detail"]
    gate.assert_untouched(head)
    assert gate.jobs() == []


def test_an_audit_changeset_needs_the_auditor_lease_and_a_human_needs_human_verify(gate) -> None:
    head = gate.head()
    unleased = submit(gate, review(gate, AIO_AB))
    assert unleased.status_code == 409 and unleased.json()["errors"][0]["code"] == "lease_required"

    token = "aiw_h_" + "reviewer"
    gate.appmod.AUTH.principals += (auth.Principal("human:reviewer", auth.token_sha256(token),
                                                   frozenset({"read", "audit"})),)
    request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": head, "reviews": [review(gate, AIO_AB)]}
    human = gate.client.post("/changesets", params={"bundle": "kb-a", "dry_run": "true"}, json=request,
                             headers={"Authorization": f"Bearer {token}"})
    assert human.status_code == 403 and "human_verify" in human.json()["detail"]
    gate.assert_untouched(head)


def test_external_mode_retires_the_codex_audit(gate) -> None:
    parent = gate.post(gate.request()).json()
    assert parent["status"] == "done" and parent["audit"] == {"mode": "external"}  # none registered

    legacy = gate.client.post(f"/jobs/{parent['id']}/audit", params={"bundle": "kb-a"}, headers=gate.headers("owner"))
    assert legacy.status_code == 409 and "GET /audit/backlog" in legacy.json()["detail"]

    # A Codex audit queued before the switch is cancelled when it comes up, never run.
    queued = I.new_audit_job(gate.writer, parent["id"], [METRIC])
    worker.submit_audit(gate.writer, parent["id"], I.job_path(gate.writer, queued["id"]))
    wait_for(lambda: gate.job(queued["id"])["status"] != "queued")
    assert gate.job(queued["id"])["status"] == "cancelled" and gate.audits == []


def test_external_mode_needs_a_usable_epoch_to_start(gate) -> None:
    for env, message in (({"AIWIKI_BACKLOG_EPOCH": ""}, "needs AIWIKI_BACKLOG_EPOCH"),
                         ({"AIWIKI_BACKLOG_EPOCH": "2026-11-03"}, "ISO 8601 time with a zone"),
                         ({"AIWIKI_BACKLOG_EPOCH": EPOCH, "AIWIKI_AUDIT_SEED_PER_DAY": "ten"},
                          "AIWIKI_AUDIT_SEED_PER_DAY")):
        with pytest.raises(RuntimeError, match=message):
            gate.app(AIWIKI_AUDIT="external", **env)
    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH)
    assert gate.client.get("/whoami", headers=gate.headers("owner")).json()["modes"]["audit"] == "external"


def test_maint_status_counts_the_backlog_in_external_mode(gate) -> None:
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers("auditor")).json()

    assert status["audit"]["mode"] == "external" and status["audit"]["pending"] == 1
    assert status["audit"]["oldest_finished"] == "2026-09-23T15:46:00Z" and status["audit"]["epoch"] == EPOCH
    # A push to a concept generated long ago waits since the push, not since that generation.
    pushed = push(gate, METRIC, gate.read(METRIC).replace("# Summary", "# Summary\n\nA hand note.", 1))
    since = datetime.fromtimestamp(int(git(gate.remote, "log", "-1", "--format=%ct", pushed)), UTC)
    entry = next(entry for entry in backlog(gate)["concepts"] if entry["path"] == METRIC)
    assert entry["since"] == since.strftime("%Y-%m-%dT%H:%M:%SZ") and entry["generated"]["at"] < EPOCH
    lease(gate)
    assert submit(gate, review(gate, AIO_AB)).status_code == 201  # the writer syncs to the push too
    status = gate.client.get("/maint/status", params={"bundle": "kb-a"}, headers=gate.headers("auditor")).json()
    assert (status["audit"]["pending"], status["audit"]["oldest_finished"]) == (1, entry["since"])


def test_disabling_audit_stops_audit_changesets_too(gate) -> None:
    """AIWIKI_DISABLE=audit is the incident switch for a misbehaving auditor: a raw POST of a
    review is refused like the backlog, while curate changesets go on."""
    gate.app(AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH, AIWIKI_DISABLE="audit")
    lease(gate)
    head = gate.head()

    assert gate.client.get("/audit/backlog", params={"bundle": "kb-a"}, headers=gate.headers("auditor")).status_code \
        == 403
    for dry_run in (True, False):
        refused = submit(gate, review(gate, AIO_AB), dry_run=dry_run)
        assert refused.status_code == 403 and "'audit' is disabled" in refused.json()["detail"], dry_run
    gate.assert_untouched(head)
    assert gate.jobs() == []
    assert gate.post(gate.request(), token="owner").status_code == 201


def test_the_read_mirror_never_serves_the_backlog(gate) -> None:
    gate.app(AIWIKI_CURATE="off")
    response = gate.client.get("/audit/backlog", params={"bundle": "kb-a"}, headers=gate.headers("auditor"))
    assert response.status_code == 403


# --- the review verbs (design §3) ----------------------------------------------------------------


def wiki(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in (*args, "--json")])
    except SystemExit as exit_:
        code = exit_.code
    out = capsys.readouterr().out
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


def test_review_verbs_keep_the_workspace_clean_and_drop_what_the_writer_refuses(gate, capsys, monkeypatch) -> None:
    tools = gate.tmp / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(workspace, "RETRY_DELAYS_S", (0, 0, 0))
    gate.connect("auditor")
    state = gate.tmp / "auditor-state"
    code, begun = wiki(capsys, "review", "begin", "--run", RUN, "--max", 1, "--state-dir", state)
    assert code == 0 and begun["backlog"] == 1, begun
    code, taken = wiki(capsys, "review", "next", "--state-dir", state)
    assert code == 0 and (taken["path"], taken["reason"]) == (AIO_AB, "generation")
    concept = gate.tmp / "auditor-state" / "reviews"
    concept = next(concept.glob("*/ws")) / AIO_AB
    assert wiki(capsys, "review", "next", "--state-dir", state)[0] == 11  # --max 1

    assert wiki(capsys, "review", "verdict", AIO_AB, "corrected", "--note", "n", "--state-dir", state)[0] == 2
    concept.write_text(narrowed(concept.read_text(encoding="utf-8")), encoding="utf-8")
    assert wiki(capsys, "review", "verdict", AIO_AB, "verified", "--note", "n", "--state-dir", state)[0] == 2
    code, recorded = wiki(capsys, "review", "verdict", AIO_AB, "corrected", "--note", "overstated", "--state-dir",
                          state)
    assert code == 0 and recorded["pending"] == 1 and "Redacted fixture body." in concept.read_text(encoding="utf-8")
    code, judged = wiki(capsys, "review", "submit", "--dry-run", "--state-dir", state)
    assert code == 0 and judged["reviews"][0]["outcome"] == "corrected" and judged["pending"] == 1
    head = gate.remote_head()

    push(gate, AIO_AB, git(gate.remote, "show", f"main:{AIO_AB}") + "\nA hand note.\n")
    code, sent = wiki(capsys, "review", "submit", "--state-dir", state)

    assert code == 6 and sent["dropped"] == [{"path": AIO_AB, "code": "conflict"}] and sent["pending"] == 0, sent
    assert gate.remote_head() != head and "A hand note." in git(gate.remote, "show", f"main:{AIO_AB}")
    code, report = wiki(capsys, "review", "end", "--run", RUN, "--state-dir", state)
    assert code == 0 and report["reviewed"] == 0 and report["dropped"] == sent["dropped"]
    assert report["backlog_remaining"] == 1 and report["lease"]["released"] is True  # the push is due again


def test_review_evidence_never_writes_where_a_packet_header_points(tmp_path) -> None:
    """Header values are collected data (a one-file packet keeps collected bytes verbatim): they
    choose what is re-read, never where the copy lands, and a match needs the Git text itself."""
    from aiwiki.cli import maint
    from aiwiki.cli import review as verbs

    upstream = tmp_path / "upstream.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    checkout = clone(upstream, tmp_path / "repos" / "control")
    (checkout / "notes.txt").write_text("attacker controlled bytes\n", encoding="utf-8")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "notes")
    commit = git(checkout, "rev-parse", "HEAD")
    blob = hashlib.sha256(b"attacker controlled bytes\n").hexdigest()
    victim = tmp_path / "home" / ".bashrc"

    def part(ref: str) -> str:
        return f"- {{ref: '{ref}', kind: git-file, remote: '{upstream}', commit: {commit}, path: notes.txt, " \
               f"sha256: {blob}}}\n"

    ws = tmp_path / "ws"
    (ws / "sources").mkdir(parents=True)
    (ws / "metrics").mkdir()
    header, escape = "---\nai_wiki_evidence: 1\nparts:\n", part("/../../../../../home/.bashrc")
    packets = {"sources/two.md": header + escape + part("../../x") + "---\n## S1\n\nattacker controlled bytes\n",
               "sources/one.md": header + escape + "---\nx\n"}
    for rel, text in packets.items():
        (ws / rel).write_text(text, encoding="utf-8")
    (ws / "sources" / ".hashes.yaml").write_text("".join(
        f"{rel}: {hashlib.sha256(text.encode()).hexdigest()}\n" for rel, text in packets.items()), encoding="utf-8")
    (ws / "metrics" / "m.md").write_text(
        "---\ntype: Metric\ntitle: M\nsources:\n- {id: a, resource: /sources/two.md}\n"
        "- {id: b, resource: /sources/one.md}\n---\n# Summary\n\nx\n", encoding="utf-8")
    state = tmp_path / "state"
    maint._write(verbs._folder(state, RUN) / "review.json",
                 {"run": RUN, "bundle": "kb", "max": 5, "taken": ["metrics/m.md"], "pending": {}, "submitted": [],
                  "dropped": [], "workspace": str(ws)})
    maint._write(verbs._pointer(state, "kb"), {"run": RUN, "bundle": "kb"})
    config = tmp_path / "maint.json"
    config.write_text(json.dumps({"repos": {"root": str(tmp_path / "repos")}}), encoding="utf-8")

    code, found = verbs.evidence(state, "kb", "metrics/m.md", config)

    assert code == 0 and not victim.exists()
    assert [(row["source"], row["status"]) for row in found["evidence"]] == [
        ("sources/two.md", "frozen"), ("sources/two.md", "match"), ("sources/two.md", "match"),
        ("sources/one.md", "frozen")]  # a one-part header is no service packet
    copies = sorted(path.name for path in (verbs._folder(state, RUN) / "evidence").iterdir())
    assert copies == ["two.md.part1", "two.md.part2"]
    packets["sources/two.md"] = packets["sources/two.md"].replace("attacker controlled bytes", "other words")
    (ws / "sources" / "two.md").write_text(packets["sources/two.md"], encoding="utf-8")
    (ws / "sources" / ".hashes.yaml").write_text("".join(
        f"{rel}: {hashlib.sha256(text.encode()).hexdigest()}\n" for rel, text in packets.items()), encoding="utf-8")
    assert [row["status"] for row in verbs.evidence(state, "kb", "metrics/m.md", config)[1]["evidence"]][1:3] == [
        "differs", "differs"]  # the header's sha256 alone proves nothing about the frozen text


def test_review_evidence_lists_related_sources_the_concept_does_not_cite(tmp_path) -> None:
    """graph-jsx was re-verified against its cited source while a newer, uncited one showed
    GraphController replacing GraphJsxController: ``related`` names frozen sources that state two
    or more of the concept's identifiers, most first, and drops names nearly every source states."""
    from aiwiki.cli import maint
    from aiwiki.cli import review as verbs

    ws = tmp_path / "ws"
    (ws / "features").mkdir(parents=True)
    (ws / "sources" / "inbox").mkdir(parents=True)
    common = " ".join(f"`common_id_{n}`" for n in range(3))
    sources = {  # cited.md and inbox/fresh.md would rank first but for their exclusions
        "sources/cited.md": "GraphJsxController serves POST /question/graph/jsx/generator from "
                            "app/controller/graphJsx.js with `questionId_field` and graphJsx\n",
        "sources/newer.md": "refactor: replace GraphJsxController with GraphController; app/controller/graphJsx.js "
                            "deleted; graphIndex and `type` are required\n",
        "sources/one-hit.md": "GraphJsxController only\n",
        "sources/drifted.md": "GraphJsxController app/controller/graphJsx.js\n",
        **{f"sources/noise-{n}.md": f"{common} questionId_field\n" for n in range(verbs.COMMON + 1)},
        "sources/inbox/fresh.md": "GraphJsxController app/controller/graphJsx.js graphJsx `questionId_field`\n",
    }
    for rel, text in sources.items():
        (ws / rel).write_text(text, encoding="utf-8")
    ledger = {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in sources.items()}  # all frozen
    ledger["sources/drifted.md"] = "0" * 64
    (ws / "sources" / ".hashes.yaml").write_text("".join(f"{rel}: {sha}\n" for rel, sha in ledger.items()),
                                                 encoding="utf-8")
    (ws / "features" / "graph.md").write_text(
        "---\ntype: Feature\ntitle: Graph JSX forwarder\naliases: [graphJsx forwarder]\nsources:\n"
        "- {id: s, resource: /sources/cited.md}\n---\n# Summary\n\n`GraphJsxController` in "
        f"app/controller/graphJsx.js forwards `questionId_field` and {common}; see [x](../sources/newer.md).\n",
        encoding="utf-8")
    state = tmp_path / "state"
    maint._write(verbs._folder(state, RUN) / "review.json",
                 {"run": RUN, "bundle": "kb", "max": 5, "taken": ["features/graph.md"], "pending": {},
                  "submitted": [], "dropped": [], "workspace": str(ws)})
    maint._write(verbs._pointer(state, "kb"), {"run": RUN, "bundle": "kb"})

    code, found = verbs.evidence(state, "kb", "features/graph.md", tmp_path / "missing.json")

    assert code == 0 and [row["status"] for row in found["evidence"]] == ["frozen"]
    assert found["related"] == [{"source": "sources/newer.md", "matches": 3,  # graphJsx: an alias's name
                                 "identifiers": ["GraphJsxController", "app/controller/graphJsx.js", "graphJsx"]}]


def test_review_next_skips_the_corrections_its_own_run_wrote(gate, capsys, monkeypatch) -> None:
    tools = gate.tmp / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    gate.connect("auditor")
    state = gate.tmp / "auditor-state"
    assert wiki(capsys, "review", "begin", "--run", RUN, "--state-dir", state)[0] == 0
    code, taken = wiki(capsys, "review", "next", "--state-dir", state)
    assert code == 0 and taken["path"] == AIO_AB
    concept = Path(taken["concept"])
    concept.write_text(narrowed(concept.read_text(encoding="utf-8")), encoding="utf-8")
    assert wiki(capsys, "review", "verdict", AIO_AB, "corrected", "--note", "overstated", "--state-dir", state)[0] == 0
    code, sent = wiki(capsys, "review", "submit", "--state-dir", state)
    assert code == 0 and sent["reviews"][0]["outcome"] == "corrected", sent
    assert (AIO_AB, "correction") in due(gate)
    from aiwiki.cli import review as verbs

    state_file = verbs._folder(state, RUN) / "review.json"
    record = json.loads(state_file.read_text(encoding="utf-8"))
    record["taken"] = []  # even untaken, the run's own correction is not offered again
    state_file.write_text(json.dumps(record), encoding="utf-8")

    assert wiki(capsys, "review", "next", "--state-dir", state)[0] == 10
    code, report = wiki(capsys, "review", "end", "--run", RUN, "--state-dir", state)
    assert code == 0 and (report["corrected"], report["verified"], report["backlog_remaining"]) == (1, 0, 1)
    assert wiki(capsys, "review", "begin", "--run", "AUD-2", "--state-dir", state)[0] == 0
    assert wiki(capsys, "review", "next", "--state-dir", state)[0] == 10  # nor another run before reviewable_at
    later(monkeypatch, 13, verbs)
    code, taken = wiki(capsys, "review", "next", "--state-dir", state)
    assert code == 0 and (taken["path"], taken["reason"]) == (AIO_AB, "correction")


def test_a_human_reviews_any_concept_through_the_cli(gate, capsys, monkeypatch) -> None:
    """The owner's token holds more than the auditor role: ``begin --as-human`` preflights a
    human reviewer, ``next --path`` takes a concept outside the backlog, and the verdict is
    human-reviewed (design §5.5). An agent's run may not pick its concepts."""
    tools = gate.tmp / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    gate.connect("owner")
    state = gate.tmp / "owner-state"
    code, refused = wiki(capsys, "review", "begin", "--run", RUN, "--state-dir", state)
    assert code == 4 and refused["failed"] == "doctor" and refused["checks"][0]["check"] == "scopes"
    code, begun = wiki(capsys, "review", "begin", "--run", RUN, "--as-human", "--state-dir", state)
    assert code == 0, begun
    assert METRIC not in [entry["path"] for entry in backlog(gate)["concepts"]]

    code, taken = wiki(capsys, "review", "next", "--path", METRIC, "--state-dir", state)
    assert code == 0 and (taken["path"], taken["reason"]) == (METRIC, "human") and taken["evidence"], taken
    assert wiki(capsys, "review", "verdict", METRIC, "verified", "--note", "S1 holds every claim",
                "--state-dir", state)[0] == 0
    code, sent = wiki(capsys, "review", "submit", "--state-dir", state)

    assert code == 0 and sent["reviews"][0]["outcome"] == "verified", sent
    metadata = gate.client.get("/cat", params={"bundle": "kb-a", "path": METRIC},
                               headers=gate.headers("owner")).json()["metadata"]
    assert (metadata["trust"], metadata["verification_current"]) == ("human-reviewed", True)
    assert wiki(capsys, "review", "end", "--run", RUN, "--state-dir", state)[0] == 0
    gate.connect("auditor")
    agent = gate.tmp / "auditor-state"
    assert wiki(capsys, "review", "begin", "--run", "AUD-2", "--state-dir", agent)[0] == 0
    assert wiki(capsys, "review", "next", "--path", METRIC, "--state-dir", agent)[0] == 2


def test_a_run_against_a_codex_writer_shadows_and_keeps_its_verdicts(gate, capsys, monkeypatch) -> None:
    """Phase 4a: while the writer runs Codex audits, the same prompt and verbs dry-run every
    verdict and ``review end`` lists what the writer would have concluded, for comparison."""
    tools = gate.tmp / "bin"
    tools.mkdir()
    (tools / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    gate.app(AIWIKI_AUDIT="codex")
    gate.connect("auditor")
    state, head = gate.tmp / "auditor-state", gate.remote_head()
    code, begun = wiki(capsys, "review", "begin", "--run", RUN, "--state-dir", state)
    assert code == 0 and begun["mode"] == "codex", begun
    code, taken = wiki(capsys, "review", "next", "--state-dir", state)
    assert code == 0 and taken["path"] == AIO_AB
    assert wiki(capsys, "review", "verdict", AIO_AB, "unverified", "--note", "lift not in S1",
                "--state-dir", state)[0] == 0

    code, sent = wiki(capsys, "review", "submit", "--state-dir", state)

    assert code == 0 and (sent["dry_run"], sent["shadow"], sent["pending"]) == (True, True, 0), sent
    code, report = wiki(capsys, "review", "end", "--run", RUN, "--state-dir", state)
    assert code == 0 and report["shadow"] is True and (report["reviewed"], report["unsubmitted"]) == (0, [])
    assert report["dry_run"] == [{"path": AIO_AB, "base": taken["base"], "verdict": "unverified",
                                  "outcome": "unverified", "downgrade": None}]
    assert gate.remote_head() == head and gate.jobs() == []
