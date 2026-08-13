"""The curation worker drains its queue strictly one job at a time (serialization is what
makes concurrent ingests safe), in FIFO order, and survives a failing job."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from aiwiki.service import worker


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    bundle = repo
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "sources" / "inbox").mkdir(parents=True)
    (bundle / "index.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "-q", "origin", "main")
    return repo, bundle, remote, _git(repo, "rev-parse", "HEAD")


def test_serial_fifo_and_survives_failure(monkeypatch) -> None:
    order: list[str] = []
    state = {"n": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(bundle: Path, source: str, job_path: Path) -> None:
        with lock:
            state["n"] += 1
            state["max"] = max(state["max"], state["n"])  # observed concurrency
        try:
            order.append(source)
            if source == "boom":
                raise RuntimeError("curate blew up")  # worker must not die
            time.sleep(0.02)
        finally:
            with lock:
                state["n"] -= 1

    monkeypatch.setattr(worker.curate, "run", fake_run)
    worker.ensure_started()
    for s in ["s0", "s1", "boom", "s2", "s3"]:
        worker.submit(Path("/b"), s, Path(f"/j/{s}.json"))
    worker._q.join()

    assert state["max"] == 1                       # never two at once
    assert order == ["s0", "s1", "boom", "s2", "s3"]  # FIFO; queue kept going past the failure


def test_ingest_and_audit_share_one_serial_fifo(monkeypatch) -> None:
    events = []
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def record(kind):
        def _run(_bundle, subject, _job_path):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            try:
                events.append((kind, subject))
                time.sleep(0.01)
            finally:
                with lock:
                    state["active"] -= 1
        return _run

    monkeypatch.setattr(worker.curate, "run", record("ingest"))
    monkeypatch.setattr(worker.audit, "run", record("audit"))
    worker.ensure_started()
    worker.submit(Path("/b"), "source-a", Path("/j/a.json"))
    worker.submit_audit(Path("/b"), "parent-a", Path("/j/audit.json"))
    worker.submit(Path("/b"), "source-b", Path("/j/b.json"))
    worker._q.join()

    assert state["max"] == 1
    assert events == [("ingest", "source-a"), ("audit", "parent-a"), ("ingest", "source-b")]


def test_recover_rolls_back_precommit_tree_and_restores_ignored_inbox(tmp_path: Path) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    source_rel = "sources/inbox/raw.md.source"
    raw = b"immutable raw\n"
    recovery = bundle / ".okf" / "recovery" / "job1.source"
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(raw)
    (bundle / "index.md").write_text("half written\n", encoding="utf-8")
    (bundle / "features").mkdir()
    (bundle / "features" / "half.md").write_text("half\n", encoding="utf-8")
    job_path = bundle / ".okf" / "jobs" / "job1.json"
    job_path.write_text(json.dumps({
        "id": "job1", "kind": "ingest", "status": "running", "phase": "prepared",
        "base_revision": base, "base_branch": "main", "source": source_rel,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "recovery_source": ".okf/recovery/job1.source",
    }), encoding="utf-8")

    ready = worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert ready is True
    assert job["status"] == "failed" and job["phase"] == "rolled_back"
    assert "rolled back to base revision" in job["error"]
    assert _git(repo, "rev-parse", "HEAD") == base
    status = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert "?? .okf/jobs/job1.json" in status
    assert all("features/half.md" not in line for line in status)
    assert (bundle / source_rel).read_bytes() == raw
    assert not recovery.exists()
    assert not (bundle / "features" / "half.md").exists()


def test_recover_rolls_back_local_unpushed_commit(tmp_path: Path) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("local job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "job commit")
    commit = _git(repo, "rev-parse", "HEAD")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")

    worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["phase"] == "rolled_back"
    assert _git(repo, "rev-parse", "HEAD") == base
    assert (bundle / "index.md").read_text(encoding="utf-8") == "base\n"


def test_recover_keeps_commit_already_pushed_before_final_job_save(tmp_path: Path) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("published job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "published job")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "validation": {"status": "passed", "error_count": 0},
        "audit": {"status": "passed", "verified_concepts": [], "unverified_concepts": []},
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")

    worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "done" and job["phase"] == "done"
    assert job["git"]["pushed"] is True
    assert job["recovered"] == "remote_contains_commit"
    assert _git(repo, "rev-parse", "HEAD") == commit
    assert (bundle / "index.md").read_text(encoding="utf-8") == "published job\n"


def test_recover_keeps_valid_durable_commit_without_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    bundle = repo
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "index.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (bundle / "index.md").write_text("local durable job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "local job")
    commit = _git(repo, "rev-parse", "HEAD")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "validation": {"status": "passed", "error_count": 0},
        "audit": {"status": "passed", "verified_concepts": [], "unverified_concepts": []},
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")

    assert worker.recover([bundle]) is True

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "done" and job["phase"] == "done"
    assert job["git"]["pushed"] is False
    assert job["recovered"] == "local_contains_commit"
    assert _git(repo, "rev-parse", "HEAD") == commit


def test_recover_pushed_job_fast_forwards_to_newer_remote_head(tmp_path: Path) -> None:
    repo, bundle, remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("published job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "job C")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.com")
    (other / "later.md").write_text("remote D\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "later D")
    remote_head = _git(other, "rev-parse", "HEAD")
    _git(other, "push", "-q", "origin", "main")

    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "validation": {"status": "passed", "error_count": 0},
        "audit": {"status": "passed", "verified_concepts": [], "unverified_concepts": []},
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")

    ready = worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert ready is True
    assert job["status"] == "done" and job["git"]["commit"] == commit
    assert _git(repo, "rev-parse", "HEAD") == remote_head
    assert (repo / "later.md").read_text(encoding="utf-8") == "remote D\n"


def test_recover_keeps_running_when_remote_containment_is_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("published job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "published job")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")
    real_git = worker.curate._git

    def fail_fetch(root: Path, *args: str, **kwargs):
        if args[:2] == ("fetch", "--quiet"):
            return subprocess.CompletedProcess(["git", "fetch"], 1, "", "network down")
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(worker.curate, "_git", fail_fetch)

    ready = worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert ready is False
    assert job["status"] == "running" and job["phase"] == "committed"
    assert "retry recovery" in job["recovery_pending"]
    assert "finished" not in job
    assert _git(repo, "rev-parse", "HEAD") == commit
    assert (bundle / "index.md").read_text(encoding="utf-8") == "published job\n"


def test_recover_does_not_reset_when_durable_branch_mismatches(tmp_path: Path) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("do not erase\n", encoding="utf-8")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "prepared",
        "base_revision": base, "base_branch": "different-branch",
    }), encoding="utf-8")

    worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "branch does not match" in job["error"]
    assert (bundle / "index.md").read_text(encoding="utf-8") == "do not erase\n"


def test_recover_never_resets_enclosing_repository_for_subdir_bundle(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    bundle = repo / "kb"
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (repo / "sibling.md").write_text("base\n", encoding="utf-8")
    (bundle / "index.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "sibling.md").write_text("must survive\n", encoding="utf-8")
    job_path = bundle / ".okf" / "jobs" / "job.json"
    job_path.write_text(json.dumps({
        "id": "job", "kind": "audit", "status": "running", "phase": "prepared",
        "base_revision": base, "base_branch": "main",
    }), encoding="utf-8")

    assert worker.recover([bundle]) is True

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "does not own its Git repository" in job["error"]
    assert (repo / "sibling.md").read_text(encoding="utf-8") == "must survive\n"


def test_recover_does_not_finish_pushed_commit_without_durable_audit_report(tmp_path: Path) -> None:
    repo, bundle, _remote, base = _repo(tmp_path)
    (bundle / "index.md").write_text("published job\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "published job")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    job_path = bundle / ".okf" / "jobs" / "audit1.json"
    job_path.write_text(json.dumps({
        "id": "audit1", "kind": "audit", "status": "running", "phase": "committed",
        "base_revision": base, "base_branch": "main", "commit": commit,
        "git": {"committed": True, "pushed": False, "commit": commit},
    }), encoding="utf-8")

    worker.recover([bundle])

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "durable audit result is incomplete" in job["error"]
    # Published state is retained even though orchestration must not treat the
    # incomplete job record as a successful audit.
    assert _git(repo, "rev-parse", "HEAD") == commit


def test_recover_reconciles_all_running_jobs_before_enqueuing_queued(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "kb"
    jobs = bundle / ".okf" / "jobs"
    jobs.mkdir(parents=True)
    # Filename order deliberately puts the queued job first; the old one-pass
    # recovery could start it before resetting the later interrupted transaction.
    (jobs / "a-queued.json").write_text(json.dumps({
        "id": "a-queued", "kind": "ingest", "status": "queued",
        "source": "sources/inbox/a.md.source",
    }), encoding="utf-8")
    (jobs / "z-running.json").write_text(json.dumps({
        "id": "z-running", "kind": "audit", "status": "running",
    }), encoding="utf-8")
    events = []
    monkeypatch.setattr(
        worker, "_reconcile_running",
        lambda _bundle, _job: events.append("reconcile") or "rolled back",
    )
    monkeypatch.setattr(
        worker, "submit",
        lambda _bundle, _source, _path: events.append("enqueue"),
    )

    worker.recover([bundle])

    assert events == ["reconcile", "enqueue"]
