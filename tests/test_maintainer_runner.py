from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aiwiki.cli import main as cli_main
from aiwiki.cli import maintain as runner


def done(job_id, parent=None, sha=None, result="passed"):
    job = {"id": job_id, "kind": "audit" if parent else "ingest", "status": "done",
           "validation": {"status": "passed"}, "commit": job_id, "changed_files": ["features/x.md"],
           "git": {"committed": True, "pushed": True, "commit": job_id}}
    if parent:
        job.update(parent_job=parent, audit={"status": result, "verified_concepts": [],
                                          "unverified_concepts": [], "corrected_concepts": []})
    else:
        job["sha256"] = sha
    return job


class Service:
    def __init__(self):
        self.jobs = {}
        self.submitted = []
        self.audited = []
        self.fail = {}
        self.audit_result = "passed"
        self.reads = []

    def __call__(self, *args, read=False):
        if args[:2] == ("config", "show"):
            return {"endpoint": "https://wiki/"}
        command, rest = args[2], args[3:]
        if command == "ingest":
            data = Path(rest[0]).read_bytes()
            self.submitted.append(data)
            job_id = f"i{len(self.submitted)}"
            job = done(job_id, sha=hashlib.sha256(data).hexdigest())
            if data in self.fail:
                job.update(status="failed", phase="rolled_back", error=self.fail[data])
                job["validation"] = {"status": "failed" if "validation" in job["error"] else "not_run"}
            self.jobs[job_id] = job
            return {"submissions": [{"job": job_id}]}
        if command == "audit":
            self.audited.append(rest[0])
            job_id = f"a{len(self.audited)}"
            self.jobs[job_id] = done(job_id, parent=rest[0], result=self.audit_result)
            return copy.deepcopy(self.jobs[job_id])
        if command == "jobs":
            self.reads.append(rest[0])
            return copy.deepcopy(self.jobs[rest[0]])
        raise AssertionError(args)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    service = Service()
    monkeypatch.setattr(runner, "cli", service)
    state = {"version": 1, "endpoint": "https://wiki", "bundle": "kb", "sources": []}
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = state_dir / "state.json"

    def add(identity, body, **kwargs):
        source = tmp_path / f"{hashlib.sha256(body).hexdigest()}.md"
        source.write_bytes(body)
        runner.add_sources(state, {"sources": [{"identity": identity, "path": str(source), **kwargs}]},
                           state_dir, "kb")
        return state["sources"][-1]

    return service, state, path, add


def test_one_failure_does_not_block_eight_sources_or_replay_completed_work(setup):
    service, state, path, add = setup
    for i in range(9):
        add(f"repo-{i}", str(i).encode())
    service.fail[b"3"] = "validation failed"
    result = runner.run_sources(state, path, "kb", poll=0)
    assert (result["done"], result["pending"]) == (8, 1)
    assert len(service.submitted) == 9 and len(service.audited) == 8
    restored = json.loads(path.read_text())
    runner.run_sources(restored, path, "kb", poll=0)
    assert len(service.submitted) == 9 and len(service.audited) == 8
    assert restored["sources"][3]["ingest"][0]["error"] == "validation failed"


def test_resume_after_ingest_only_launches_missing_audit(setup):
    service, state, path, add = setup
    entry = add("repo", b"evidence")
    entry["ingest"] = [done("original", sha=entry["sha256"])]
    result = runner.run_sources(state, path, "kb", poll=0)
    assert result["done"] == 1 and service.submitted == []
    assert service.audited == ["original"]


def test_running_job_is_resumed_without_duplicate_submission(setup):
    service, state, path, add = setup
    entry = add("repo", b"evidence")
    entry["ingest"] = [{"id": "running", "status": "running"}]
    service.jobs["running"] = done("running", sha=entry["sha256"])
    assert runner.run_sources(state, path, "kb", poll=0)["done"] == 1
    assert service.submitted == [] and service.audited == ["running"]


def test_same_source_versions_stay_ordered_and_old_pending_survives_manifest(setup):
    service, state, path, add = setup
    add("repo", b"old")
    service.fail[b"old"] = "validation failed"
    runner.run_sources(state, path, "kb", poll=0)
    add("repo", b"new")
    add("independent", b"other")
    result = runner.run_sources(state, path, "kb", poll=0)
    assert result["pending"] == 2 and result["done"] == 1
    assert service.submitted == [b"old", b"other"]
    assert "earlier version" in state["sources"][1]["error"]


def test_transient_failure_retries_after_cooldown_across_restarts(setup, monkeypatch):
    service, state, path, add = setup
    now = [10000]
    monkeypatch.setattr(runner.time, "time", lambda: now[0])
    add("repo", b"source")
    service.fail[b"source"] = "curation timed out after 900s"
    runner.run_sources(state, path, "kb", poll=0)
    assert len(service.submitted) == 1
    runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0)
    assert len(service.submitted) == 1
    now[0] += 300
    runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0)
    assert len(service.submitted) == 2
    # A later recovery is not permanently blocked by two historical attempts.
    now[0] += 300
    service.fail.clear()
    restored = json.loads(path.read_text())
    assert runner.run_sources(restored, path, "kb", poll=0)["done"] == 1
    assert len(restored["sources"][0]["ingest"]) == 3
    assert service.submitted == [b"source"] * 3 and len(service.audited) == 1


@pytest.mark.parametrize("error", ["login required", "disk full", "permission denied", "validation failed"])
def test_nontransient_failures_do_not_blindly_retry(setup, error):
    service, state, path, add = setup
    add("repo", b"source")
    service.fail[b"source"] = error
    runner.run_sources(state, path, "kb", poll=0)
    assert len(service.submitted) == 1


def test_needs_attention_is_completed_and_not_retried(setup):
    service, state, path, add = setup
    add("repo", b"source")
    service.audit_result = "needs_attention"
    assert runner.run_sources(state, path, "kb", poll=0)["done"] == 1
    runner.run_sources(state, path, "kb", poll=0)
    assert len(service.audited) == 1


def test_submission_unknown_is_persisted_and_never_blindly_duplicated(setup, monkeypatch):
    service, state, path, add = setup
    add("repo", b"source")
    calls = []

    def unknown(*args, **kwargs):
        calls.append(args)
        raise runner.Pending("response lost")

    monkeypatch.setattr(runner, "cli", unknown)
    assert runner.run_sources(state, path, "kb", poll=0)["pending"] == 1
    assert json.loads(path.read_text())["sources"][0]["submitting"] == "ingest"
    runner.run_sources(state, path, "kb", poll=0)
    assert len(calls) == 1


def test_frozen_source_tampering_is_blocked(setup):
    service, state, path, add = setup
    entry = add("repo", b"source")
    Path(entry["path"]).write_bytes(b"tampered")
    assert runner.run_sources(state, path, "kb", poll=0)["pending"] == 1
    assert not service.submitted


@pytest.mark.parametrize("suffix", [".md", ".json", ""])
def test_new_frozen_source_uses_short_basename_and_preserves_extension(setup, tmp_path, suffix):
    service, state, path, _add = setup
    source = tmp_path / f"upstream{suffix}"
    source.write_bytes(b"source evidence")
    sha = hashlib.sha256(b"source evidence").hexdigest()

    runner.add_sources(state, {"sources": [{"identity": "repo", "path": str(source)}]},
                       path.parent, "kb")

    frozen = Path(state["sources"][0]["path"])
    assert frozen == path.parent / "sources" / sha / f"evidence{suffix}"
    assert frozen.read_bytes() == b"source evidence"
    assert state["sources"][0]["sha256"] == sha
    assert runner.run_sources(state, path, "kb", poll=0)["done"] == 1
    assert service.submitted == [b"source evidence"]


def test_existing_frozen_path_is_not_migrated_and_identities_stay_separate(setup, tmp_path):
    service, state, path, _add = setup
    data = b"same evidence"
    sha = hashlib.sha256(data).hexdigest()
    legacy = path.parent / "sources" / f"{sha}.md"
    legacy.parent.mkdir()
    legacy.write_bytes(data)
    state["sources"].append({"identity": "first", "sha256": sha, "path": str(legacy),
                             "ingest": [], "audit": [], "status": "pending"})
    source = tmp_path / "new.md"
    source.write_bytes(data)

    runner.add_sources(state, {"sources": [
        {"identity": "first", "path": str(source)},
        {"identity": "second", "path": str(source)},
    ]}, path.parent, "kb")

    assert len(state["sources"]) == 2
    assert Path(state["sources"][0]["path"]) == legacy
    assert Path(state["sources"][1]["path"]) == path.parent / "sources" / sha / "evidence.md"
    assert [row["identity"] for row in state["sources"]] == ["first", "second"]
    assert runner.run_sources(state, path, "kb", poll=0)["done"] == 2
    assert service.submitted == [data, data]


def test_import_prior_receipts_and_audit_gap(setup):
    service, state, path, add = setup
    sha = hashlib.sha256(b"source").hexdigest()
    service.jobs["historical"] = done("historical", sha=sha)
    entry = add("repo", b"source", ingest_job="historical")
    assert entry["ingest"][0]["id"] == "historical"
    assert runner.run_sources(state, path, "kb", poll=0)["done"] == 1
    assert not service.submitted


def test_import_rejects_wrong_source_job(setup):
    service, state, path, add = setup
    service.jobs["wrong"] = done("wrong", sha="not-the-source")
    with pytest.raises(ValueError, match="does not match"):
        add("repo", b"source", ingest_job="wrong")


@pytest.mark.parametrize("field,value", [("pushed", False), ("committed", False), ("commit", "wrong")])
def test_receipt_rejects_missing_or_wrong_git_result(field, value):
    job = done("job", "parent")
    job["git"][field] = value
    with pytest.raises(runner.Pending):
        runner.receipt(job, parent="parent")


def test_receipt_accepts_no_concept_noop_but_not_unrelated_concepts():
    job = done("audit", "parent")
    job.update(commit=None, reason="no_concepts_to_audit", git={}, changed_files=[])
    runner.receipt(job, parent="parent")
    job["audit"]["verified_concepts"] = ["unrelated.md"]
    with pytest.raises(runner.Pending):
        runner.receipt(job, parent="parent")


def test_read_retry_is_bounded_and_never_retries_auth(monkeypatch):
    calls, sleeps = [], []

    def fail(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="server rejected the request (status 503)", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fail)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    with pytest.raises(runner.Pending):
        runner.cli("jobs", "id", "--json", read=True)
    assert len(calls) == 4 and sleeps == [5, 15, 30]
    calls.clear()
    with pytest.raises(runner.Pending):
        runner.cli("audit", "id", "--json")
    assert len(calls) == 1


def test_poll_deadline_preserves_running_job_for_next_run(setup):
    service, state, path, add = setup
    entry = add("repo", b"source")
    entry["ingest"] = [{"id": "running", "status": "running"}]
    service.jobs["running"] = copy.deepcopy(entry["ingest"][0])
    assert runner.run_sources(state, path, "kb", poll=0, wait=0)["pending"] == 1
    assert service.submitted == []
    assert entry["ingest"][0]["id"] == "running"


def test_pending_audit_sweep_closes_missing_audit_without_reingesting(tmp_path, monkeypatch):
    service = Service()
    parent = done("orphan", sha="original-sha")
    parent["concept_files"] = ["features/x.md"]
    service.jobs["orphan"] = parent

    def cli(*args, **kwargs):
        if "--pending-audit" in args:
            return {"jobs": [parent], "shown": 1, "total": 1, "truncated": False, "unscoped": 7}
        return service(*args, **kwargs)

    monkeypatch.setattr(runner, "cli", cli)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"sources": []}')
    state_dir = tmp_path / "state"
    args = ["-b", "kb", "maintain", "--manifest", str(manifest), "--state-dir", str(state_dir), "--audit-pending"]
    assert cli_main.main(args) == 0
    assert service.submitted == [] and service.audited == ["orphan"]
    assert cli_main.main(args) == 0
    assert service.audited == ["orphan"]
    state = json.loads((state_dir / "state.json").read_text())
    assert state["pending_audit_discovery"]["unscoped"] == 7


def test_state_rejects_changed_server_before_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "cli", Service())
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "version": 1, "endpoint": "https://other", "bundle": "kb", "sources": []}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"sources": []}')
    with pytest.raises(ValueError, match="different endpoint"):
        runner.run(manifest=manifest, state_dir=state_dir, bundle="kb")


def test_known_rejected_submission_does_not_leave_unknown_intent(setup, monkeypatch):
    service, state, path, add = setup
    add("repo", b"source")
    monkeypatch.setattr(runner, "cli", lambda *a, **k: (_ for _ in ()).throw(
        runner.Pending("server rejected the request (status 409)", rejected=True)))
    runner.run_sources(state, path, "kb", poll=0)
    assert "submitting" not in state["sources"][0]


def test_capacity_stops_batch_preserves_sources_and_recovers_without_replay(setup, monkeypatch):
    service, state, path, add = setup
    now = [10000]
    monkeypatch.setattr(runner.time, "time", lambda: now[0])
    add("already-done", b"done")
    runner.run_sources(state, path, "kb", poll=0)
    for i in range(3):
        add(f"repo-{i}", str(i).encode())
    service.fail[b"0"] = "You've hit your usage limit. Try again at Sep 16th, 2026 7:31 PM."
    result = runner.run_sources(state, path, "kb", poll=0)
    assert (result["done"], result["pending"]) == (1, 3)
    assert result["writer_retry"]["kind"] == "capacity"
    assert service.submitted == [b"done", b"0"]
    assert all(Path(e["path"]).exists() for e in state["sources"])
    runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0)
    assert service.submitted == [b"done", b"0"]
    now[0] += 3600
    service.fail.clear()
    restored = json.loads(path.read_text())
    result = runner.run_sources(restored, path, "kb", poll=0)
    assert result["done"] == 4 and result["pending"] == 0 and "writer_retry" not in result
    assert service.submitted == [b"done", b"0", b"0", b"1", b"2"]
    assert [j["status"] for j in restored["sources"][1]["ingest"]] == ["failed", "done"]
    assert len(service.audited) == 4


def test_retry_now_skips_capacity_cooldown_but_only_probes_once(setup):
    service, state, path, add = setup
    add("repo", b"source")
    add("later", b"later")
    service.fail[b"source"] = "HTTP 429: Too many requests"
    runner.run_sources(state, path, "kb", poll=0)
    runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0, retry_now=True)
    assert service.submitted == [b"source", b"source"]
    service.fail.clear()
    assert runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0, retry_now=True)["done"] == 2
    assert service.submitted == [b"source", b"source", b"source", b"later"]


@pytest.mark.parametrize("error", ["login required", "disk full", "permission denied", "validation failed",
                                   "unrecognized model output", "invalid source 5fc6667503e429f"])
def test_retry_now_cannot_bypass_hard_failures(setup, error):
    service, state, path, add = setup
    add("repo", b"source")
    service.fail[b"source"] = error
    runner.run_sources(state, path, "kb", poll=0)
    runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0, retry_now=True)
    assert len(service.submitted) == 1 and not service.audited


@pytest.mark.parametrize("kind", ["ingest", "audit"])
def test_retry_now_requires_confirmed_rollback_and_correct_receipt(setup, kind):
    service, state, path, add = setup
    entry = add("repo", b"source")
    ingest = done("original", sha=entry["sha256"])
    entry["ingest"] = [ingest]
    failed = ingest if kind == "ingest" else done("audit", parent="original")
    if kind == "audit":
        entry["audit"] = [failed]
    failed.update(status="failed", phase="recovery_pending", error="connection reset")
    failed["validation"] = {"status": "not_run"}
    assert runner.run_sources(state, path, "kb", poll=0, retry_now=True)["pending"] == 1
    assert not service.submitted and not service.audited
    failed["phase"] = "rolled_back"
    if kind == "ingest":
        failed["sha256"] = "wrong"
    else:
        failed["parent_job"] = "wrong"
    assert runner.run_sources(state, path, "kb", poll=0, retry_now=True)["pending"] == 1
    assert not service.submitted and not service.audited


def test_audit_capacity_recovery_does_not_repeat_ingest(setup):
    service, state, path, add = setup
    entry = add("repo", b"source")
    entry["ingest"] = [done("original", sha=entry["sha256"])]
    failed = done("old-audit", parent="original")
    failed.update(status="failed", phase="rolled_back", error="You've hit your usage limit.",
                  validation={"status": "not_run"})
    entry["audit"] = [failed]
    add("later", b"later")
    assert runner.run_sources(state, path, "kb", poll=0)["pending"] == 2
    assert not service.submitted and not service.audited
    restored = json.loads(path.read_text())
    assert runner.run_sources(restored, path, "kb", poll=0, retry_now=True)["done"] == 2
    assert service.submitted == [b"later"] and service.audited[0] == "original"
    assert restored["sources"][0]["audit"][0] == failed


def test_old_quota_failures_automatically_resume_from_existing_v1_state(setup):
    service, state, path, add = setup
    for i in range(10):
        entry = add(f"repo-{i}", str(i).encode())
        old = {"id": f"old-{i}", "kind": "ingest", "sha256": entry["sha256"],
               "status": "failed", "phase": "rolled_back", "validation": {"status": "not_run"},
               "error": "You've hit your usage limit. Try again at Sep 16th, 2026 7:31 PM.",
               "finished": "2000-01-01T00:00:00Z"}
        entry["ingest"] = [old]
    result = runner.run_sources(state, path, "kb", poll=0)
    assert result["done"] == 10 and result["pending"] == 0
    assert len(service.submitted) == 10 and len(service.audited) == 10
    assert all([j["status"] for j in e["ingest"]] == ["failed", "done"] for e in state["sources"])


def test_new_evidence_is_frozen_during_cooldown_and_resume_needs_no_manifest(setup):
    service, state, path, add = setup
    add("repo", b"source")
    service.fail[b"source"] = "You've hit your usage limit."
    runner.run_sources(state, path, "kb", poll=0)
    source = path.parent / "extra.md"
    source.write_text("extra")
    manifest = path.parent / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"identity": "extra", "path": str(source)}]}))
    result = runner.run(manifest=manifest, state_dir=path.parent, bundle="kb", poll=0)
    assert result["pending"] == 2 and len(service.submitted) == 1
    source.unlink()
    service.fail.clear()
    assert runner.run(manifest=None, state_dir=path.parent, bundle="kb", poll=0, retry_now=True)["done"] == 2
    assert service.submitted == [b"source", b"source", b"extra"]


def test_same_source_new_version_waits_for_transient_recovery(setup):
    service, state, path, add = setup
    add("repo", b"old")
    add("repo", b"new")
    add("independent", b"other")
    service.fail[b"old"] = "status 503: service unavailable"
    result = runner.run_sources(state, path, "kb", poll=0)
    assert result["done"] == 1 and service.submitted == [b"old", b"other"]
    service.fail.clear()
    result = runner.run_sources(json.loads(path.read_text()), path, "kb", poll=0, retry_now=True)
    assert result["done"] == 3 and service.submitted == [b"old", b"other", b"old", b"new"]


def test_cli_maintain_formats_and_missing_state_error(setup, monkeypatch, capsys):
    service, state, path, add = setup
    add("repo", b"source")
    runner.save(path, state)
    args = ["-b", "kb", "maintain", "--state-dir", str(path.parent), "--poll-seconds", "0"]
    assert cli_main.main(args) == 0
    out = capsys.readouterr().out
    assert "done: 1" in out and "sources[1]{identity,status,action,retry_at}" in out
    assert cli_main.main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["done"] == 1
    assert len(service.submitted) == 1 and len(service.audited) == 1
    assert cli_main.main(["-b", "kb", "maintain", "--state-dir", str(path.parent / "empty"), "--json"]) == 1
    assert "no saved state" in json.loads(capsys.readouterr().out)["error"]


def test_state_lock_prevents_overlapping_recovery(setup):
    service, state, path, add = setup
    runner.save(path, state)
    with (path.parent / "runner.lock").open("a") as lock:
        runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        with pytest.raises(runner.Pending, match="another runner"):
            runner.run(manifest=None, state_dir=path.parent, bundle="kb", retry_now=True)
    assert not service.submitted and not service.audited
