"""The runtime independently validates agent output before it is committed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aiwiki.runtime import curate


def _run_job(tmp_path: Path, monkeypatch, validation_errors: list[str]) -> dict:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    job_path = bundle / "job.json"
    job_path.write_text(json.dumps({"source": "sources/inbox/n.md", "status": "queued"}), encoding="utf-8")

    monkeypatch.setattr(curate, "_repo_root", lambda _bundle: bundle)
    monkeypatch.setattr(curate, "_pre_sync", lambda _root: {"synced": True})
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: validation_errors)
    monkeypatch.setattr(
        curate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="curated", stderr=""),
    )
    curate.run(bundle, "sources/inbox/n.md", job_path)
    return json.loads(job_path.read_text(encoding="utf-8"))


def test_validation_failure_blocks_commit(tmp_path: Path, monkeypatch) -> None:
    committed = []
    monkeypatch.setattr(curate, "_working_files", lambda _root: ["concepts/bad.md"])
    monkeypatch.setattr(curate, "_commit_and_push", lambda *args: committed.append(args))

    job = _run_job(tmp_path, monkeypatch, ["concepts/bad.md: missing # Citations section"])
    assert job["status"] == "failed"
    assert job["validation"] == {
        "status": "failed",
        "error_count": 1,
        "errors": ["concepts/bad.md: missing # Citations section"],
    }
    assert job["changed_files"] == ["concepts/bad.md"]
    assert committed == []


def test_validation_pass_records_commit_and_changed_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        curate,
        "_commit_and_push",
        lambda *_args: {
            "committed": True,
            "pushed": True,
            "commit": "abc123",
            "changed_files": ["concepts/new.md", "sources/n.md"],
        },
    )

    job = _run_job(tmp_path, monkeypatch, [])
    assert job["status"] == "done"
    assert job["validation"] == {"status": "passed", "error_count": 0}
    assert job["commit"] == "abc123"
    assert job["changed_files"] == ["concepts/new.md", "sources/n.md"]
