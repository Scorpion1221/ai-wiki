"""The changeset writer transaction against a bare remote (design §2.5 G5–G14, §10.1).

A failed fetch or pre-sync rebase refuses the changeset as transient instead of applying
it to a stale base; a push-time rebase that merges cleanly but moves a base is caught by
the CAS recheck; a kill between commit and push is reconciled by ``recover()``.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiwiki.cli.maintain import receipt
from aiwiki.engine import scan_sources
from aiwiki.engine.document import parse_document
from aiwiki.engine.gen_indexes import generate_indexes
from aiwiki.runtime import audit, changeset, curate
from aiwiki.service import ingest as I
from aiwiki.service import worker

LIVE = Path(__file__).parent / "fixtures" / "live_bundle"
ACTOR = "process:ai-wiki-maintainer"
METRIC = "metrics/plugin-install-first-payment-funnel-2026-09.md"
AI_STUDY = "experiments/ai-study-deferred-login-ab.md"
EVIDENCE = b"# Funnel status 2026-09-24\n\nThe plugin funnel moved.\n"
EVIDENCE_ID = "funnel-status-2026-09-24"
NOTES = "\n## Notes\n\n" + "".join(f"- note {index}\n" for index in range(1, 9))


class _Killed(BaseException):
    """The writer process dies here; nothing below this frame runs."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _clone(remote: Path, path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "t@local")
    _git(path, "config", "user.name", "t")
    return path


@pytest.fixture
def repos(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A bare remote holding a valid bundle, and the writer's clone of it."""
    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    seed = _clone(remote, tmp_path / "seed")
    shutil.copytree(LIVE, seed, dirs_exist_ok=True)
    (seed / METRIC).write_text((seed / METRIC).read_text(encoding="utf-8") + NOTES, encoding="utf-8")
    (seed / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Bundle\n', encoding="utf-8")
    (seed / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")  # as the live bundle
    for path in sorted(seed.rglob("*.md")):
        for resource in re.findall(r"resource: (/sources/\S+)", path.read_text(encoding="utf-8")):
            stub = seed / resource.lstrip("/")
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_bytes(f"frozen evidence {stub.name}\n".encode())
    generate_indexes(seed)
    assert scan_sources.main([str(seed), "--commit"]) == 0
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    return _clone(remote, tmp_path / "writer"), remote


def _other_writer_pushes(remote: Path, rel: str, edit) -> str:
    """Another clone commits and pushes first; returns its commit."""
    other = _clone(remote, remote.parent / f"other-{len(list(remote.parent.glob('other-*')))}")
    path = other / rel
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
    _git(other, "commit", "-qam", f"other: {rel}")
    _git(other, "push", "-q", "origin", "main")
    return _git(other, "rev-parse", "HEAD")


def _cited(text: str) -> str:
    """The concept with this changeset's packet cited and one new claim."""
    lines = text.splitlines(keepends=True)
    start = lines.index("sources:\n")
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]+:|---", lines[i]))
    lines.insert(end, f"- {{id: {EVIDENCE_ID}, resource: evidence:packet}}\n")
    return "".join(lines).replace("# Summary\n\n", f"# Summary\n\nThe funnel moved.[^{EVIDENCE_ID}]\n\n", 1)


def _request(writer: Path, *files: dict, **extra) -> dict:
    return {
        "schema": changeset.SCHEMA, "kind": "curate", "intent": "evidence",
        "base_revision": _git(writer, "rev-parse", "HEAD"), "work_items": [],
        "evidence": {"id": EVIDENCE_ID, "upload": {"filename": "status.md",
                                                   "content_b64": base64.b64encode(EVIDENCE).decode()}},
        "files": list(files) or [_put(writer, METRIC, _cited(_read(writer, METRIC)))],
        "message": "curate: plugin funnel status", **extra,
    }


def _read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


def _put(writer: Path, rel: str, content: str) -> dict:
    return {"path": rel, "op": "put", "base": changeset.content_hash(_read(writer, rel)), "content": content}


def _run(writer: Path, request: dict) -> dict:
    job_path = writer / ".okf" / "jobs" / "cs0000000001.json"
    curate.run_changeset(writer, job_path, request, actor=ACTOR)
    return _job(writer)


def _job(writer: Path) -> dict:
    return json.loads((writer / ".okf" / "jobs" / "cs0000000001.json").read_text(encoding="utf-8"))


def _remote_head(remote: Path) -> str:
    return _git(remote, "rev-parse", "main")


def _untouched(writer: Path, head: str) -> None:
    """The writer clone is back at ``head`` with a clean tree and no stray packet."""
    assert _git(writer, "rev-parse", "HEAD") == head
    assert _git(writer, "status", "--porcelain") == ""
    assert not list((writer / "sources" / "inbox").glob("*"))


def _on_push(monkeypatch, around):
    """Route every ``git push`` of the writer through ``around(push)``; returns the real helper."""
    real = curate._git

    def git(root, *args, **kwargs):
        if args[:1] == ("push",):
            return around(lambda: real(root, *args, **kwargs))
        return real(root, *args, **kwargs)

    monkeypatch.setattr(curate, "_git", git)
    return real


# --- G5: strict pre-sync -------------------------------------------------------------------


def test_pre_sync_is_best_effort_unless_strict(repos) -> None:
    writer, _remote = repos
    _git(writer, "remote", "set-url", "origin", str(writer.parent / "missing.git"))
    assert curate._pre_sync(writer) == {"synced": False, "note": "fetch failed"}
    assert curate._pre_sync(writer, strict=True) == {"synced": False, "note": "fetch failed", "refused": True}


def test_fetch_failure_is_refused_as_transient(repos) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)
    _git(writer, "remote", "set-url", "origin", str(writer.parent / "missing.git"))

    job = _run(writer, request)

    assert job["status"] == "failed" and job["pre_sync"]["note"] == "fetch failed"
    assert job["failure"]["class"] == "transient" and job["failure"]["stage"] == "pre_sync"
    assert job["failure"]["retryable"] is True
    assert job["mode"] == "changeset" and "commit" not in job
    _untouched(writer, head)
    assert _remote_head(remote) == head


def test_rebase_failure_is_refused_as_transient(repos) -> None:
    writer, remote = repos
    # An unpushed local commit that conflicts with the remote: a stale base, not a sync.
    (writer / METRIC).write_text(_read(writer, METRIC).replace("- note 8", "- note 8 (local)"), encoding="utf-8")
    _git(writer, "commit", "-qam", "stale local edit")
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)
    upstream = _other_writer_pushes(remote, METRIC, lambda text: text.replace("- note 8", "- note 8 (upstream)"))

    job = _run(writer, request)

    assert job["status"] == "failed" and job["pre_sync"]["note"].startswith("rebase skipped")
    assert (job["failure"]["class"], job["failure"]["stage"]) == ("transient", "pre_sync")
    _untouched(writer, head)
    assert not (writer / ".git" / "rebase-merge").exists() and not (writer / ".git" / "rebase-apply").exists()
    assert _remote_head(remote) == upstream


# --- G6–G13: the gate decides what is applied ------------------------------------------------


def test_changeset_commits_and_pushes_exactly_the_gate_bytes(repos) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    deprecate = {"path": AI_STUDY, "op": "deprecate", "base": changeset.content_hash(_read(writer, AI_STUDY)),
                 "superseded_by": METRIC, "reason": "folded into the funnel metric"}
    request = _request(writer, _put(writer, METRIC, _cited(_read(writer, METRIC))), deprecate,
                       work_items=["it_3f2a9c1b7d10"], run="WAIO-612\nForged: trailer")
    dry_run = changeset.evaluate(writer, request, actor=ACTOR, now=datetime.now(UTC))
    assert dry_run["status"] == "would_apply", dry_run["errors"]

    job = _run(writer, request)

    assert job["status"] == "done", job.get("error") or job.get("errors")
    assert job["git"]["pushed"] is True and _remote_head(remote) == job["commit"]
    assert _git(writer, "rev-parse", "HEAD~1") == head
    # Deprecations are committed but never handed to an audit.
    assert (job["concept_files"], job["deprecated_files"]) == ([METRIC], [AI_STUDY])
    assert audit.concept_files(writer, job) == [METRIC]
    snapshot = job["source_snapshot"]
    assert snapshot == dry_run["source"] and (writer / snapshot).read_bytes() == EVIDENCE
    assert audit._find_source(writer, job) == snapshot
    metric = parse_document(_read(writer, METRIC)).frontmatter
    assert metric["generated"]["by"] == ACTOR and metric["status"] == "stable"
    assert metric["sources"][-1] == {"id": EVIDENCE_ID, "resource": "/" + snapshot}
    assert parse_document(_read(writer, AI_STUDY)).frontmatter["status"] == "deprecated"
    # Up to the trusted stamp time, the committed concepts are the dry-run's bytes.
    stamp = re.compile(r"at: '?\d{4}-\d\d-\d\dT[\d:]+Z'?")
    for rel in (METRIC, AI_STUDY):
        assert stamp.sub("at: T", _read(writer, rel)) == stamp.sub("at: T", dry_run["files"][rel])
    message = _git(writer, "log", "-1", "--format=%B")
    assert message.splitlines()[0] == "curate: plugin funnel status"
    trailers = _git(writer, "log", "-1", "--format=%(trailers:only,unfold)").splitlines()
    assert trailers == ["Changeset: cs0000000001", f"Principal: {ACTOR}", "Work-Items: it_3f2a9c1b7d10",
                        "Run: WAIO-612 Forged: trailer"]
    assert f"Changeset cs0000000001 curated {snapshot}" in _read(writer, "log.md")
    assert _git(writer, "status", "--porcelain") == ""
    assert not list((writer / "sources" / "inbox").glob("*"))


def test_a_gate_rejection_changes_nothing(repos) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    stale = {**_put(writer, METRIC, _cited(_read(writer, METRIC))), "base": "ch1:" + "0" * 64}

    job = _run(writer, _request(writer, stale))

    assert (job["status"], job["http_status"]) == ("rejected", 409)
    assert [error["code"] for error in job["errors"]] == ["conflict"]
    assert job["conflicts"][0]["path"] == METRIC and job["failure"]["class"] == "conflict"
    assert "files" not in job and job["phase"] == "rolled_back"
    _untouched(writer, head)
    assert _remote_head(remote) == head


def test_an_unknown_base_revision_is_rejected(repos) -> None:
    writer, _remote = repos
    head = _git(writer, "rev-parse", "HEAD")

    job = _run(writer, _request(writer, base_revision="0" * 40))

    assert (job["status"], job["http_status"]) == ("rejected", 409)
    assert [error["code"] for error in job["errors"]] == ["unknown_base"]
    _untouched(writer, head)


def test_a_malformed_request_is_rejected_before_anything_is_written(repos) -> None:
    writer, _remote = repos
    head = _git(writer, "rev-parse", "HEAD")

    job = _run(writer, {**_request(writer), "files": []})

    assert (job["status"], job["http_status"], job["failure"]["class"]) == ("rejected", 400, "input")
    assert "source" not in job and "started" not in job
    _untouched(writer, head)


def test_a_noop_changeset_is_done_without_a_commit(repos) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")

    job = _run(writer, _request(writer, _put(writer, METRIC, _read(writer, METRIC))))

    assert (job["status"], job["noop"], job["commit"]) == ("done", True, None)
    assert job["noop_files"] == [METRIC] and job["concept_files"] == []
    _untouched(writer, head)
    assert _remote_head(remote) == head


def test_on_done_runs_under_the_lock_before_the_receipt_reads_done(repos) -> None:
    writer, remote = repos
    seen = []

    def on_done(job: dict) -> None:
        seen.append((job["status"], _job(writer)["status"], _remote_head(remote) == job["commit"]))
        job["closed_items"] = ["it_3f2a9c1b7d10"]

    for request in (_request(writer), _request(writer, _put(writer, AI_STUDY, _read(writer, AI_STUDY)))):
        curate.run_changeset(writer, writer / ".okf" / "jobs" / "cs0000000001.json", request, actor=ACTOR,
                             on_done=on_done)
        assert _job(writer)["closed_items"] == ["it_3f2a9c1b7d10"]  # saved with the done receipt
        receipt(_job(writer))  # committed, then the documented no-change exception

    # Committed and noop alike: the hook sees done while the file on disk does not yet.
    assert seen == [("done", "running", True), ("done", "running", False)]
    rejected = _run(writer, {**_request(writer), "base_revision": "0" * 40})
    assert rejected["status"] == "rejected" and len(seen) == 2


def test_an_audit_changeset_is_rejected_as_input(repos) -> None:
    writer, _remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    review = {"path": METRIC, "base": changeset.content_hash(_read(writer, METRIC)), "verdict": "verified",
              "note": "ok"}
    request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": head, "reviews": [review]}
    assert changeset.check_request(request) == []

    job = _run(writer, request)

    assert (job["status"], job["http_status"], job["failure"]["class"]) == ("rejected", 400, "input")
    _untouched(writer, head)


def test_an_older_source_with_the_same_bytes_keeps_the_packet_snapshot(repos) -> None:
    writer, _remote = repos
    older = "sources/a-earlier-upload.md.source"  # sorts before the packet's own name
    (writer / older).write_bytes(EVIDENCE)
    assert scan_sources.main([str(writer), "--commit"]) == 0
    _git(writer, "add", "-A")
    _git(writer, "commit", "-qm", "an earlier upload of the same bytes")
    _git(writer, "push", "-q", "origin", "main")
    request = _request(writer)
    dry_run = changeset.evaluate(writer, request, actor=ACTOR, now=datetime.now(UTC))

    job = _run(writer, request)

    assert job["status"] == "done", job.get("validation")
    assert job["source_snapshot"] == dry_run["source"] != older
    assert audit._find_source(writer, job) == job["source_snapshot"]


def test_a_concept_path_starting_with_a_dash_is_logged_not_parsed(repos) -> None:
    writer, remote = repos
    content = (
        f"---\ntype: Metric\ntitle: Dash probe\ndescription: A probe concept\ntags: [metric]\n"
        f"sources:\n- {{id: {EVIDENCE_ID}, resource: evidence:packet}}\n---\n# Summary\n\n"
        f"The funnel moved.[^{EVIDENCE_ID}]\n\n[^{EVIDENCE_ID}]: status file\n"
    )

    job = _run(writer, _request(writer, {"path": "-probe.md", "op": "put", "base": None, "content": content}))

    assert job["status"] == "done", job.get("error") or job.get("errors")
    assert _remote_head(remote) == job["commit"] and "files: -probe.md" in _read(writer, "log.md")


def test_the_commit_message_is_one_redacted_line() -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEpAIBAAKCAQEA7bq9Zx1Qk3" * 4 + "\n-----END RSA PRIVATE KEY-----"
    request = {"message": key, "run": "x" * 100 + " ghp_" + "A" * 40, "evidence": {"id": EVIDENCE_ID}}

    message = curate._changeset_message("cs1", request, ACTOR)

    # Redacted before the 120-character cut, which would otherwise split both out of their rules.
    assert "MIIE" not in message and "ghp_" not in message and "AAAA" not in message
    assert message.splitlines()[0] == "<redacted:private_key>"
    nul = curate._changeset_message("cs1", {"message": "curate\x00: nul\x1b[0m", "evidence": {"id": "e"}}, ACTOR)
    assert nul.splitlines()[0] == "curate : nul [0m"


def test_a_control_character_in_the_message_still_commits(repos) -> None:
    writer, remote = repos

    job = _run(writer, _request(writer, message="curate\x00: nul"))

    assert job["status"] == "done", job.get("error")
    assert _git(remote, "log", "-1", "--format=%s", "main") == "curate : nul"


# --- G14: a push-time rebase is re-judged ------------------------------------------------------


def test_commit_and_push_recheck_stops_a_rebased_push(repos) -> None:
    writer, remote = repos
    upstream = _other_writer_pushes(remote, AI_STUDY, lambda text: text + "\nUpstream note.\n")
    (writer / "extra.md").write_text("# Extra\n", encoding="utf-8")
    seen = []

    out = curate._commit_and_push(writer, "ingest: extra", recheck=lambda: seen.append(True) or [{"code": "x"}])

    assert seen == [True] and out["committed"] is True and out["pushed"] is False
    assert out["recheck_errors"] == [{"code": "x"}]
    assert _remote_head(remote) == upstream


def test_a_competing_push_elsewhere_rebases_and_passes_the_recheck(repos, monkeypatch) -> None:
    writer, remote = repos
    request = _request(writer)
    pushes = []

    def around(push):
        if not pushes:
            pushes.append(_other_writer_pushes(remote, AI_STUDY, lambda text: text + "\nUpstream note.\n"))
        return push()

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    assert job["status"] == "done", job.get("errors") or job.get("git")
    assert _remote_head(remote) == job["commit"] and _git(writer, "rev-parse", "HEAD~1") == pushes[0]
    assert "Upstream note." in _read(writer, AI_STUDY) and "The funnel moved." in _read(writer, METRIC)


def test_a_competing_push_to_the_same_file_is_caught_by_the_cas_recheck(repos, monkeypatch) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)
    pushes = []

    def around(push):
        if not pushes:  # lines far from the changeset's edit: Git merges them cleanly
            pushes.append(_other_writer_pushes(remote, METRIC, lambda text: text.replace("- note 8", "- note 8 (v2)")))
        return push()

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    assert (job["status"], job["http_status"], job["failure"]["class"]) == ("rejected", 409, "conflict")
    assert job["git"]["note"] == "push rejected; the rebased tree failed the recheck"
    assert "recheck_errors" not in job["git"]
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("conflict", METRIC)]
    assert job["conflicts"] == [{"path": METRIC, "base": request["files"][0]["base"],
                                 "current": changeset.content_hash(_git(remote, "show", f"main:{METRIC}") + "\n")}]
    assert _remote_head(remote) == pushes[0]
    _untouched(writer, head)
    assert "The funnel moved." not in _git(remote, "show", f"main:{METRIC}")


def test_a_link_target_removed_upstream_is_caught_by_the_recheck(repos, monkeypatch) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    link = "See [the study](../experiments/ai-study-deferred-login-ab.md)."
    linked = _cited(_read(writer, METRIC)).replace("Redacted fixture body.", f"Redacted fixture body. {link}")
    request = _request(writer, _put(writer, METRIC, linked))
    pushes = []

    def around(push):
        if not pushes:  # another writer removes the linked concept; Git rebases cleanly
            other = _clone(remote, remote.parent / "other-rm")
            _git(other, "rm", "-q", AI_STUDY)
            _git(other, "commit", "-qm", "other: remove the study")
            _git(other, "push", "-q", "origin", "main")
            pushes.append(_git(other, "rev-parse", "HEAD"))
        return push()

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    # The race is a conflict to pull and re-apply, not the agent's malformed output.
    assert (job["status"], job["http_status"]) == ("rejected", 409)
    assert (job["failure"]["class"], job["failure"]["stage"]) == ("conflict", "git")
    assert [(error["code"], error.get("path")) for error in job["errors"]] == [("broken_link", METRIC)]
    assert "conflicts" not in job and _remote_head(remote) == pushes[0]
    _untouched(writer, head)


def test_a_text_conflict_at_push_time_is_a_409_conflict(repos, monkeypatch) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)
    pushes = []

    def around(push):
        if not pushes:  # upstream edits the very lines the changeset edits: Git cannot rebase
            pushes.append(_other_writer_pushes(
                remote, METRIC, lambda text: text.replace("# Summary\n\n", "# Summary\n\nUpstream line.\n\n", 1)))
        return push()

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    assert job["git"]["note"] == curate.REBASE_CONFLICT
    assert (job["status"], job["http_status"]) == ("rejected", 409)
    assert (job["failure"]["class"], job["failure"]["stage"]) == ("conflict", "git")
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("conflict", METRIC)]
    assert [conflict["path"] for conflict in job["conflicts"]] == [METRIC]
    assert _remote_head(remote) == pushes[0]
    _untouched(writer, head)


@pytest.mark.parametrize("stamp", ["verified", "deprecated"])
def test_an_upstream_stamp_on_a_written_file_is_a_conflict(repos, monkeypatch, stamp) -> None:
    """A verification or deprecation merged onto new content would publish it as audited
    or retired. The content hash ignores both, so only the bytes the gate judged can tell."""
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)
    audited = "- {by: process:ai-wiki-adversarial-audit, at: 2026-09-17T21:28:57Z}\n"
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    edit = {
        "verified": lambda text: text.replace(audited, audited + f"- {{by: 'human:alice', at: '{at}'}}\n", 1),
        "deprecated": lambda text: text.replace("status: stable\n", "status: deprecated\n", 1),
    }[stamp]
    pushes = []

    def around(push):
        if not pushes:  # frontmatter lines away from the changeset's: Git merges them cleanly
            pushes.append(_other_writer_pushes(remote, METRIC, edit))
        return push()

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    assert job["git"]["note"] == "push rejected; the rebased tree failed the recheck"
    assert (job["status"], job["http_status"], job["failure"]["class"]) == ("rejected", 409, "conflict")
    assert [(error["code"], error["path"]) for error in job["errors"]] == [("conflict", METRIC)]
    assert job["conflicts"][0]["current"] == job["conflicts"][0]["base"]  # the content itself did not move
    assert job["errors"][0]["message"] == "the concept's verified, status or generated fields changed upstream"
    assert _remote_head(remote) == pushes[0]
    assert "The funnel moved." not in _git(remote, "show", f"main:{METRIC}")
    _untouched(writer, head)


def test_a_lost_push_acknowledgement_is_recorded_as_done(repos, monkeypatch) -> None:
    writer, remote = repos
    request = _request(writer)
    pushes = []

    def around(push):  # the remote takes the first push, but the writer sees it fail
        result = push()
        pushes.append(result.returncode)
        if len(pushes) > 1:
            return result
        return subprocess.CompletedProcess(result.args, 1, "", "fatal: the remote end hung up unexpectedly")

    _on_push(monkeypatch, around)
    job = _run(writer, request)

    assert job["status"] == "done", job.get("errors") or job.get("git")
    assert pushes == [0] and job["git"]["pushed"] is True
    assert job["commit"] == _remote_head(remote) == _git(writer, "rev-parse", "HEAD")
    assert "The funnel moved." in _git(remote, "show", f"main:{METRIC}")


# --- crash recovery ------------------------------------------------------------------------------


def test_kill_after_commit_before_push_is_rolled_back_by_recover(repos, monkeypatch) -> None:
    writer, remote = repos
    head = _git(writer, "rev-parse", "HEAD")
    request = _request(writer)

    def killed(_push):
        raise _Killed

    real = _on_push(monkeypatch, killed)
    with pytest.raises(_Killed):
        _run(writer, request)
    monkeypatch.setattr(curate, "_git", real)
    interrupted = _job(writer)
    assert (interrupted["status"], interrupted["phase"]) == ("running", "committed")
    assert _git(writer, "rev-parse", "HEAD") == interrupted["commit"] != head

    assert worker.recover([writer]) is True

    job = _job(writer)
    assert (job["status"], job["phase"]) == ("failed", "rolled_back")
    assert job["failure"]["class"] == "interrupted" and job["failure"]["retryable"] is True
    assert _git(writer, "rev-parse", "HEAD") == head and _remote_head(remote) == head
    assert _git(writer, "status", "--porcelain") == ""
    assert not (writer / ".okf" / "recovery").exists() or not list((writer / ".okf" / "recovery").iterdir())


def test_recover_never_hands_a_staged_changeset_to_the_codex_curator(repos, monkeypatch) -> None:
    writer, _remote = repos
    request = _request(writer)
    staged = []
    # Killed after the packet is staged but before the transaction marks the job running.
    monkeypatch.setattr(curate, "_transaction", lambda *args, **kwargs: staged.append(args[1]) or _die())
    job_path = writer / ".okf" / "jobs" / "cs0000000001.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({"id": "cs0000000001", "kind": "ingest", "status": "queued"}), encoding="utf-8")
    with pytest.raises(_Killed):
        curate.run_changeset(writer, job_path, request, actor=ACTOR)
    assert _job(writer)["status"] == "queued" and _job(writer)["source"] == staged[0]
    submitted = []
    monkeypatch.setattr(worker, "submit", lambda *args: submitted.append(args))
    monkeypatch.setattr(worker, "submit_changeset", lambda *args: submitted.append(("changeset", *args)))

    assert worker.recover([writer]) is True

    # No request was persisted in .okf/changesets to run it from: the job fails, so a
    # resend runs afresh instead of hitting a job that would stay queued forever.
    job = _job(writer)
    assert submitted == [] and (job["status"], job["failure"]["class"]) == ("failed", "interrupted")

    # With its request persisted, recovery queues it as a changeset again.
    I.changeset_path(writer, "cs0000000001").parent.mkdir(parents=True)
    I.changeset_path(writer, "cs0000000001").write_text(json.dumps({"request": request}), encoding="utf-8")
    job_path.write_text(json.dumps({**job, "status": "queued"}), encoding="utf-8")

    assert worker.recover([writer]) is True

    assert submitted == [("changeset", writer, job_path)]


def _die():
    raise _Killed


def test_kill_after_push_is_reconciled_as_done_by_recover(repos, monkeypatch) -> None:
    writer, remote = repos
    request = _request(writer)

    def killed_after(push):
        push()
        raise _Killed

    real = _on_push(monkeypatch, killed_after)
    with pytest.raises(_Killed):
        _run(writer, request)
    monkeypatch.setattr(curate, "_git", real)
    assert _job(writer)["phase"] == "committed"

    assert worker.recover([writer]) is True

    job = _job(writer)
    assert (job["status"], job["recovered"]) == ("done", "remote_contains_commit")
    assert _remote_head(remote) == job["commit"] == _git(writer, "rev-parse", "HEAD")
    assert job["concept_files"] == [METRIC]
    assert _git(writer, "status", "--porcelain") == ""
