"""The runtime independently validates agent output before it is committed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aiwiki.runtime import curate


def _run_job(tmp_path: Path, monkeypatch, validation_errors: list[str], refresh=None) -> dict:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    job_path = bundle / "job.json"
    job_path.write_text(json.dumps({"source": "sources/inbox/n.md", "status": "queued"}), encoding="utf-8")

    monkeypatch.setattr(curate, "_repo_root", lambda _bundle: bundle)
    monkeypatch.setattr(curate, "_pre_sync", lambda _root: {"synced": True})
    monkeypatch.setattr(curate, "_refresh_visualization", refresh or (lambda _root, _bundle: None))
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


def test_validation_pass_refreshes_visualization_before_commit(tmp_path: Path, monkeypatch) -> None:
    events = []
    refresh = lambda *_args: events.append("render") or {  # noqa: E731
        "path": "viz.html", "concepts": 2, "edges": 1, "bytes": 100,
    }
    monkeypatch.setattr(
        curate,
        "_commit_and_push",
        lambda *_args: events.append("commit") or {
            "committed": True,
            "pushed": True,
            "commit": "abc123",
            "changed_files": ["concepts/new.md", "viz.html"],
        },
    )

    job = _run_job(tmp_path, monkeypatch, [], refresh=refresh)

    assert events == ["render", "commit"]
    assert job["visualization"] == {"path": "viz.html", "concepts": 2, "edges": 1, "bytes": 100}
    assert job["changed_files"] == ["concepts/new.md", "viz.html"]


def test_refresh_visualization_is_opt_in_and_preserves_display_name(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    bundle = root / "knowledge"
    bundle.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (bundle / "concept.md").write_text(
        """---
type: Feature
title: First
description: First concept
tags: [demo]
status: draft
confidence: low
source_ref: source.md
---

# First
""",
        encoding="utf-8",
    )

    assert curate._refresh_visualization(root, bundle) is None
    curate.generate_visualization(bundle, root / "viz.html", bundle_name="Custom Wiki Name")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True)
    (bundle / "second.md").write_text(
        """---
type: Feature
title: Second
description: Second concept
tags: [demo]
status: draft
confidence: low
source_ref: source.md
---

# Second
""",
        encoding="utf-8",
    )

    result = curate._refresh_visualization(root, bundle)
    html = (root / "viz.html").read_text(encoding="utf-8")

    assert result is not None and result["concepts"] == 2
    assert result["path"] == "viz.html"
    assert 'window.BUNDLE_NAME = "Custom Wiki Name";' in html
    assert '"label": "Second"' in html


def test_failed_inbox_source_is_excluded_from_later_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    bundle = root / "knowledge"
    inbox = bundle / "sources" / "inbox"
    inbox.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True)

    (inbox / "failed-job.md").write_text("must not leak\n", encoding="utf-8")
    curate._exclude_inbox(root, bundle)
    concept = bundle / "features" / "new.md"
    concept.parent.mkdir(parents=True)
    concept.write_text("# New\n", encoding="utf-8")

    result = curate._commit_and_push(root, "ingest: current job")

    assert result["committed"] is True
    assert result["changed_files"] == ["knowledge/features/new.md"]
    committed = subprocess.run(
        ["git", "-C", str(root), "show", "--pretty=format:", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == ["knowledge/features/new.md"]
    assert (inbox / "failed-job.md").is_file()
