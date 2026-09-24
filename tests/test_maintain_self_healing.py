"""`ai-wiki maintain` heals itself from real 0.2.x failure receipts and never deadlocks.

Fixtures under tests/fixtures/receipts are production job receipts (`ai-wiki jobs <id> --json`)
with agent summaries and host paths redacted.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from aiwiki.cli import main as cli_main
from aiwiki.cli import maintain as runner
from aiwiki.runtime.failure import classify
from aiwiki.version import VERSION

FIXTURES = Path(__file__).parent / "fixtures" / "receipts"
EXPERIMENT = "solvely/daily/experiment-measurement-20260920"
H5 = "solvely/daily/h5-checkout-recovery-20260920"


def real(job_id: str) -> dict:
    return json.loads((FIXTURES / f"{job_id}.json").read_text(encoding="utf-8"))


def done(job_id, parent=None, sha=None):
    job = {"id": job_id, "kind": "audit" if parent else "ingest", "status": "done", "phase": "done",
           "validation": {"status": "passed"}, "commit": job_id, "changed_files": ["features/x.md"],
           "git": {"committed": True, "pushed": True, "commit": job_id}}
    if parent:
        job.update(parent_job=parent, audit={"status": "passed", "verified_concepts": [],
                                             "unverified_concepts": [], "corrected_concepts": []})
    else:
        job["sha256"] = sha
    return job


class Writer:
    """A writer that dedupes nothing and fails the bytes it is told to fail."""

    def __init__(self):
        self.jobs, self.ingested, self.audited, self.fail = {}, [], [], {}
        self.build = None
        self.health_build = None
        self.health_calls = 0

    def __call__(self, *args, read=False):
        if args[:2] == ("config", "show"):
            return {"endpoint": "https://wiki/"}
        command, rest = args[2], args[3:]
        if command == "health":
            self.health_calls += 1
            return {"service_version": VERSION, "build": self.health_build}
        if command == "ingest":
            data = Path(rest[0]).read_bytes()
            self.ingested.append(data)
            job = done(f"i{len(self.ingested)}", sha=hashlib.sha256(data).hexdigest())
            if self.build:
                job["service"] = {"version": VERSION, "build": self.build}
            if data in self.fail:
                job.update(status="failed", phase="rolled_back", commit=None, git={}, **self.fail[data])
            self.jobs[job["id"]] = job
            return {"submissions": [{"job": job["id"]}]}
        if command == "audit":
            self.audited.append(rest[0])
            job = done(f"a{len(self.audited)}", parent=rest[0])
            self.jobs[job["id"]] = job
            return copy.deepcopy(job)
        if command == "jobs" and rest[0] == "--pending-audit":
            return {"jobs": [], "shown": 0, "total": 0, "truncated": False, "unscoped": 0}
        if command == "jobs":
            return copy.deepcopy(self.jobs[rest[0]])
        raise AssertionError(args)


@pytest.fixture
def writer(monkeypatch) -> Writer:
    service = Writer()
    monkeypatch.setattr(runner, "cli", service)
    return service


def _state_dir(tmp_path: Path, sources: list[dict]) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    runner.save(state_dir / "state.json",
                {"version": 1, "endpoint": "https://wiki", "bundle": "kb", "sources": sources})
    return state_dir


def _frozen(state_dir: Path, data: bytes, identity: str) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    path = state_dir / "sources" / sha / "evidence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"identity": identity, "sha256": sha, "path": str(path), "ingest": [], "audit": [], "status": "pending"}


def _maintain(state_dir: Path, *extra: str, capsys) -> tuple[int, dict]:
    code = cli_main.main(["-b", "kb", "maintain", "--state-dir", str(state_dir), "--poll-seconds", "0",
                          "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("job_id,kind,stage", [
    ("02325bea5bc3", "model_output", "validation"),  # ingest: invalid YAML in 3 concepts
    ("3399e2a8cea8", "model_output", "validation"),  # ingest: mistyped 64-hex snapshot path
    ("a658b12f157b", "timeout", "agent"),            # ingest: 900s wall clock, no diagnostics
    ("71ea85ca9c20", "model_output", "validation"),  # audit: trusted-window writer bug
    ("e94c8b707aea", "model_output", "validation"),  # audit: same writer bug on 0.2.8
    ("9de6461bab99", "model_output", "validation"),  # audit: OKFDocumentError repr, invalid YAML
])
def test_real_failed_receipts_are_bounded_retries_not_manual_repairs(job_id, kind, stage):
    job = real(job_id)
    failure = classify(job)
    assert (failure["class"], failure["retryable"], failure["retry_after_s"], failure["stage"]) == (
        kind, True, 300, stage)
    assert runner.retry_kind(job) == kind
    # The old client answered "requires repair" for every one of these (maintain.py:88/102).
    stage_name = job.get("kind", "ingest")
    entry = {"sha256": job.get("sha256"), stage_name: [job]}
    assert runner._retry_failure(entry, stage_name, job.get("parent_job"), job, lambda: None) == failure


def test_real_manual_recovery_receipts_pass_the_import_gates():
    # 6f4b6f97e4b2 re-curated the same bytes as 02325bea5bc3/a658b12f157b; f7dddac6e389 audited it.
    ingest, reviewed = real("6f4b6f97e4b2"), real("f7dddac6e389")
    assert ingest["sha256"] == real("02325bea5bc3")["sha256"] == real("a658b12f157b")["sha256"]
    runner.receipt(ingest)
    runner.receipt(reviewed, parent=ingest["id"])


def test_waio_587_ledger_replay_supersedes_stuck_versions_and_finishes_new_ones(tmp_path, writer, capsys):
    """Replay of the 09-21..09-23 deadlock: 65 done / 4 pending, nothing could move."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    completed = _frozen(state_dir, b"repo scan 20260919", "solvely/daily/repo-scan-20260919")
    completed.update(status="done", ingest=[done("d1", sha=completed["sha256"])], audit=[done("d2", "d1")])
    old_experiment = {
        "identity": EXPERIMENT, "sha256": real("02325bea5bc3")["sha256"],
        "path": str(state_dir / "sources" / real("02325bea5bc3")["sha256"] / "evidence.md"),
        "ingest": [real("02325bea5bc3"), real("a658b12f157b")], "audit": [], "status": "pending",
        "error": "ingest requires repair: bundle validation failed with 3 error(s)",
    }
    old_h5 = {
        "identity": H5, "sha256": real("3399e2a8cea8")["sha256"],
        "path": str(state_dir / "sources" / real("3399e2a8cea8")["sha256"] / "evidence.md"),
        "ingest": [real("3399e2a8cea8")], "audit": [], "status": "pending",
        "error": "ingest requires repair: bundle validation failed with 4 error(s)",
    }
    new_experiment = _frozen(state_dir, b"experiment measurement, window regenerated 20260921", EXPERIMENT)
    new_h5 = _frozen(state_dir, b"h5 checkout recovery, window regenerated 20260921", H5)
    _state_dir(tmp_path, [completed, old_experiment, old_h5, new_experiment, new_h5])

    code, result = _maintain(state_dir, capsys=capsys)

    assert code == 0, result
    assert (result["done"], result["pending"], result["needs_repair"], result["superseded"]) == (3, 0, 0, 2)
    state = json.loads((state_dir / "state.json").read_text())
    old_experiment, old_h5, new_experiment, new_h5 = state["sources"][1:]
    assert old_experiment["status"] == old_h5["status"] == "superseded"
    assert old_experiment["superseded_by"] == new_experiment["sha256"]
    assert old_h5["superseded_by"] == new_h5["sha256"]
    # Every historical receipt survives; nothing was re-run for the stale versions.
    assert [job["id"] for job in old_experiment["ingest"]] == ["02325bea5bc3", "a658b12f157b"]
    assert [job["id"] for job in old_h5["ingest"]] == ["3399e2a8cea8"]
    assert writer.ingested == [b"experiment measurement, window regenerated 20260921",
                               b"h5 checkout recovery, window regenerated 20260921"]
    assert new_experiment["status"] == new_h5["status"] == "done" and len(writer.audited) == 2
    # A second run is a no-op.
    assert _maintain(state_dir, capsys=capsys)[0] == 0 and len(writer.ingested) == 2


def test_after_the_cap_one_extra_attempt_follows_a_writer_build_change(tmp_path, writer, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    source = _frozen(state_dir, b"evidence", "repo")
    _state_dir(tmp_path, [source])
    writer.fail[b"evidence"] = {"error": "bundle validation failed with 1 error(s)",
                                "validation": {"status": "failed"}}
    writer.build = "0000aaa"
    for _ in range(4):
        code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert len(writer.ingested) == 3 and code == 3 and result["needs_repair"] == 1
    assert writer.health_calls >= 1  # consulted only at the cap; the mirror reported no build

    writer.health_build = "0000aaa"  # same build: no fix deployed
    assert _maintain(state_dir, "--retry-now", capsys=capsys)[0] == 3 and len(writer.ingested) == 3

    writer.health_build = writer.build = "1111bbb"  # a fix was deployed
    code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert len(writer.ingested) == 4 and code == 3  # the extra attempt ran, and failed on the new build
    assert _maintain(state_dir, "--retry-now", capsys=capsys)[0] == 3 and len(writer.ingested) == 4
    writer.fail.clear()
    writer.health_build = "2222ccc"
    code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert code == 0 and result["done"] == 1 and len(writer.ingested) == 5


def test_mirror_writer_build_skew_grants_one_extra_attempt_per_reported_build(tmp_path, writer, capsys):
    # /health is the read mirror; the writer stamps receipts. Deployed separately, they can
    # disagree for days, which must not turn the cap into an endless daily LLM pass.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, b"evidence", "repo"), _frozen(state_dir, b"other", "other")])
    writer.fail = {data: {"error": "bundle validation failed with 1 error(s)", "validation": {"status": "failed"}}
                   for data in (b"evidence", b"other")}
    writer.build, writer.health_build = "writer-w2", "mirror-w1"
    for _ in range(10):
        code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert writer.ingested.count(b"evidence") == 4 and code == 3 and result["needs_repair"] == 2
    writer.health_build = "mirror-w3"  # a later deploy earns one more attempt, not more
    for _ in range(3):
        code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert writer.ingested.count(b"evidence") == 5 and code == 3


def test_short_and_full_sha_of_one_build_earn_no_extra_attempt(tmp_path, writer, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, b"evidence", "repo")])
    writer.fail[b"evidence"] = {"error": "bundle validation failed with 1 error(s)",
                                "validation": {"status": "failed"}}
    writer.build, writer.health_build = "68a5e53", "68a5e53d66f1c0ffee"
    for _ in range(5):
        code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert len(writer.ingested) == 3 and code == 3


def test_lost_post_of_an_old_version_is_superseded_not_re_curated(tmp_path, writer, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old = _frozen(state_dir, b"old window", "repo")
    old.update(submitting="ingest", ingest=[{"id": "f1", "kind": "ingest", "status": "failed", "phase": "rolled_back",
                                             "sha256": old["sha256"], "error": "curation timed out after 900s"}])
    never_answered = _frozen(state_dir, b"older window", "repo-2")
    never_answered["submitting"] = "ingest"
    _state_dir(tmp_path, [old, never_answered, _frozen(state_dir, b"new window", "repo"),
                          _frozen(state_dir, b"newer window", "repo-2")])
    code, result = _maintain(state_dir, capsys=capsys)
    assert code == 0 and writer.ingested == [b"new window", b"newer window"] and len(writer.audited) == 2
    state = json.loads((state_dir / "state.json").read_text())
    assert [row["status"] for row in state["sources"]] == ["superseded", "superseded", "done", "done"]
    assert not any("submitting" in row for row in state["sources"])


def test_unconfirmed_rollback_is_never_superseded_silently(tmp_path, writer, capsys):
    # A restart mid-push leaves the ingest commit on the remote with the job failed: the
    # older version must stay visible for repair while the newer one still runs.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old = _frozen(state_dir, b"old window", "repo")
    old["ingest"] = [{"id": "pushed", "kind": "ingest", "status": "failed", "phase": "pushed",
                      "sha256": old["sha256"],
                      "error": "interrupted by service restart: remote contains ingest commit, but durable "
                               "concept scope is incomplete"}]
    _state_dir(tmp_path, [old, _frozen(state_dir, b"new window", "repo")])
    code, result = _maintain(state_dir, capsys=capsys)
    assert code == 3 and (result["needs_repair"], result["done"], result["superseded"]) == (1, 1, 0)
    assert result["sources"][0]["action"] == "repair" and "remote contains" in result["sources"][0]["error"]
    assert writer.ingested == [b"new window"]


def test_a_source_that_only_ever_fails_capacity_stops_blocking_the_batch(tmp_path, writer, capsys):
    # A receipt misclassified as capacity (frozen into job.failure by the writer) must not
    # freeze every other source forever: after CAPACITY_BATCH_STOPS stops it cools down alone.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, f"source {i}".encode(), f"repo-{i}") for i in range(3)])
    writer.fail[b"source 0"] = {"error": "adversarial reviewer failed",
                                "failure": {"class": "capacity", "retryable": True, "retry_after_s": 3600,
                                            "stage": "agent", "detail": "rate-limit"}}
    for _ in range(runner.CAPACITY_BATCH_STOPS):
        code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
        assert code == 1 and result["writer_retry"]["kind"] == "capacity" and result["done"] == 0
    code, result = _maintain(state_dir, "--retry-now", capsys=capsys)
    assert code == 1 and "writer_retry" not in result and (result["done"], result["pending"]) == (2, 1)
    assert writer.ingested == [b"source 0"] * 4 + [b"source 1", b"source 2"]
    assert result["sources"][0]["retry_at"]  # still retried on its own cooldown


def test_unavailable_reads_before_processing_are_pending_not_fatal(tmp_path, writer, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, b"daily", "daily")])
    extra = tmp_path / "extra.md"
    extra.write_bytes(b"extra")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [{"identity": "extra", "path": str(extra), "ingest_job": "lost"}]}))

    def cli(*args, **kwargs):
        if "--pending-audit" in args or "lost" in args:
            raise runner.Pending("server rejected the request (status 502)")
        return writer(*args, **kwargs)

    monkeypatch.setattr(runner, "cli", cli)
    code, result = _maintain(state_dir, "--audit-pending", "--manifest", str(manifest), capsys=capsys)
    # Both sources still ran; the unavailable reads are reported and redone next run.
    assert code == 1 and result["done"] == 2 and writer.ingested == [b"daily", b"extra"]
    assert [w.split(":")[0] for w in result["warnings"]] == ["extra", "pending-audit discovery unavailable"]
    monkeypatch.setattr(runner, "cli", writer)
    code, result = _maintain(state_dir, "--audit-pending", capsys=capsys)
    assert code == 0 and "warnings" not in result

    with (state_dir / "runner.lock").open("a") as lock:
        runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        code, result = _maintain(state_dir, capsys=capsys)
    assert code == 1 and "another runner" in result["error"]


def test_structured_capacity_failure_stops_the_batch(tmp_path, writer, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sources = [_frozen(state_dir, f"source {i}".encode(), f"repo-{i}") for i in range(3)]
    _state_dir(tmp_path, sources)
    writer.fail[b"source 0"] = {"error": "adversarial reviewer failed",
                                "failure": {"class": "capacity", "retryable": True, "retry_after_s": 3600,
                                            "stage": "agent", "detail": "429"}}
    code, result = _maintain(state_dir, capsys=capsys)
    assert code == 1 and result["writer_retry"]["kind"] == "capacity" and result["pending"] == 3
    assert writer.ingested == [b"source 0"]


@pytest.mark.parametrize("fail,expected", [
    ({}, 0),
    ({"error": "stream disconnected before completion"}, 1),
    ({"error": "login required"}, 3),
])
def test_exit_codes_and_summary_counts(tmp_path, writer, capsys, fail, expected):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, b"a", "a"), _frozen(state_dir, b"b", "b")])
    if fail:
        writer.fail[b"a"] = {"validation": {"status": "not_run"}, **fail}
    code, result = _maintain(state_dir, capsys=capsys)
    assert code == expected
    assert set(result) >= {"done", "pending", "needs_repair", "superseded", "sources"}
    assert result["done"] == (2 if not fail else 1)  # the independent source always completes
    with pytest.raises(SystemExit) as usage:
        cli_main.main(["-b", "kb", "maintain", "--state-dir", str(state_dir), "--import-only"])
    assert usage.value.code == 2 and "--manifest" in capsys.readouterr().out


def test_status_is_offline_and_works_while_a_runner_holds_the_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "CONFIG", tmp_path / "config.json")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    blocked = _frozen(state_dir, b"x", "x")
    blocked.update(status="needs_repair", error="ingest auth failure is not retryable")
    _state_dir(tmp_path, [blocked])
    monkeypatch.setattr(runner, "cli", lambda *a, **k: pytest.fail("--status must not call the writer"))
    monkeypatch.setattr(cli_main, "_api", lambda *a, **k: pytest.fail("--status must not use the network"))
    with (state_dir / "runner.lock").open("a") as lock:
        runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        code, result = _maintain(state_dir, "--status", capsys=capsys)
    assert code == 3 and result["needs_repair"] == 1 and result["sources"][0]["action"] == "repair"
    assert cli_main.main(["maintain", "--state-dir", str(state_dir), "--status"]) == 3
    assert "needs_repair: 1" in capsys.readouterr().out
    assert cli_main.main(["maintain", "--state-dir", str(tmp_path / "none"), "--status", "--json"]) == 2
    assert "no saved state" in json.loads(capsys.readouterr().out)["error"]


def test_import_only_freezes_and_imports_without_submitting(tmp_path, writer, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old = _frozen(state_dir, b"old", "repo")
    old.update(status="superseded", superseded_by="new-sha",
               ingest=[{"id": "failed", "kind": "ingest", "status": "failed", "phase": "rolled_back",
                        "sha256": old["sha256"], "error": "curation timed out after 900s"}])
    _state_dir(tmp_path, [old])
    writer.jobs["recovered"] = done("recovered", sha=old["sha256"])
    writer.jobs["reviewed"] = done("reviewed", parent="recovered")
    evidence = tmp_path / "old.md"
    evidence.write_bytes(b"old")
    extra = tmp_path / "extra.md"
    extra.write_bytes(b"extra")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": [
        {"identity": "repo", "path": str(evidence), "ingest_job": "recovered", "audit_job": "reviewed"},
        {"identity": "extra", "path": str(extra)},
    ]}))
    code, result = _maintain(state_dir, "--manifest", str(manifest), "--import-only", capsys=capsys)
    assert code == 1 and result["pending"] == 2 and not writer.ingested and not writer.audited
    state = json.loads((state_dir / "state.json").read_text())
    assert "superseded_by" not in state["sources"][0]
    assert Path(state["sources"][1]["path"]).read_bytes() == b"extra"
    # The next ordinary run adopts the imported receipts instead of re-running them.
    code, result = _maintain(state_dir, capsys=capsys)
    assert code == 0 and writer.ingested == [b"extra"] and writer.audited == ["i1"]


def test_malformed_pending_audit_orphan_does_not_abort_the_run(tmp_path, writer, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_dir(tmp_path, [_frozen(state_dir, b"daily", "daily")])
    orphan = done("orphan", sha="orphan-sha")
    broken = done("broken", sha="broken-sha")
    broken["git"]["pushed"] = False
    writer.jobs["orphan"] = orphan

    def cli(*args, **kwargs):
        if "--pending-audit" in args:
            return {"jobs": [broken, orphan], "shown": 2, "total": 2, "truncated": False}
        return writer(*args, **kwargs)

    monkeypatch.setattr(runner, "cli", cli)
    code, result = _maintain(state_dir, "--audit-pending", capsys=capsys)
    assert code == 0 and result["done"] == 2 and writer.audited == ["i1", "orphan"]
    state = json.loads((state_dir / "state.json").read_text())
    assert [row["id"] for row in state["pending_audit_discovery"]["invalid"]] == ["broken"]


@pytest.mark.parametrize("service_version,compatible", [
    (VERSION, True), (".".join(VERSION.split(".")[:2]) + ".99", True), ("9.9.0", False), (None, False),
])
def test_health_json_reports_client_compatibility_by_major_minor(tmp_path, monkeypatch, capsys,
                                                                 service_version, compatible):
    monkeypatch.setattr(cli_main, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(cli_main, "_api", lambda *a, **k: {"bundle": "kb", "service_version": service_version})
    assert cli_main.main(["health", "--json"]) == 0
    health = json.loads(capsys.readouterr().out)
    assert health["client_version"] == VERSION and health["compatible"] is compatible
