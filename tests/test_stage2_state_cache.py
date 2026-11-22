"""Cache-correctness and equivalence tests for Stage 2 state indexing (ingest & maint_state)."""
from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from aiwiki.service import ingest as I
from aiwiki.service import maint_state as M


def _make_item_spec(topic: str, body: str, *, priority: int = 70, name: str = "note.md") -> dict:
    return {
        "origin": {"kind": "repo", "commit": "abc1234"},
        "topic_key": topic,
        "priority": priority,
        "brief": f"brief for {topic}",
        "files": [{
            "name": name,
            "content_b64": base64.b64encode(body.encode()).decode(),
            "origin": {"kind": "git-file", "path": f"tasks/{name}"},
        }],
    }


def test_ingest_job_cache_hits_on_unchanged_and_tracks_every_mutation(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "kb"
    jobs_dir = bundle / ".okf" / "jobs"
    jobs_dir.mkdir(parents=True)

    j1 = I.new_changeset_job(
        bundle, {"request": {}, "evidence_files": []},
        principal="process:ai-wiki-maintainer", changeset_sha256="d" * 64,
    )
    p1 = I.job_path(bundle, j1["id"])
    # Simulate an older historical file outside the racy-clean window so it enters cache.
    monkeypatch.setattr(I, "_RACY_NS", -1)
    assert len(I.changeset_jobs(bundle, "process:ai-wiki-maintainer")) == 1
    assert os.fspath(p1) in I._JOB_CACHE

    # Returned copy from find_changeset_job must not corrupt cache if caller mutates it.
    found = I.find_changeset_job(bundle, "process:ai-wiki-maintainer", "d" * 64)
    assert found is not None
    found["status"] = "corrupted_by_caller"
    assert I.find_changeset_job(bundle, "process:ai-wiki-maintainer", "d" * 64)["status"] == "queued"

    # Atomic save_job changes inode and invalidates cache immediately.
    j1["status"] = "done"
    I.save_job(bundle, j1)
    assert I.find_changeset_job(bundle, "process:ai-wiki-maintainer", "d" * 64)["status"] == "done"

    # External in-place overwrite with racy guard restored MUST be noticed even within 1ms!
    monkeypatch.setattr(I, "_RACY_NS", 2_000_000_000)
    j1["status"] = "failed"
    p1.write_text(json.dumps(j1), encoding="utf-8")
    assert I.find_changeset_job(bundle, "process:ai-wiki-maintainer", "d" * 64) is None

    # External delete disappears immediately.
    p1.unlink()
    assert I.changeset_jobs(bundle) == []

    # active_jobs preserves stem fallback when a job file omits "id".
    (jobs_dir / "legacy_run.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    assert I.active_jobs(bundle) == ["legacy_run"]


def test_maint_state_item_cache_and_batch_enqueue_equivalence(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "kb"
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")

    # Allow old-file caching path to exercise _ITEM_CACHE.
    monkeypatch.setattr(M, "_RACY_NS", -1)
    M.acquire_lease(bundle, "maintainer", principal="process:ai-wiki-maintainer", run="r1")

    res = M.enqueue(
        bundle,
        [
            _make_item_spec("topic:a", "v1"),
            _make_item_spec("topic:a", "v1"),  # exact duplicate within batch
            _make_item_spec("topic:a", "v2", priority=80),  # merge into topic:a within batch
            _make_item_spec("topic:a", "v1"),  # duplicate of merged version key within batch
            _make_item_spec("topic:b", "b1", priority=60),
        ],
        principal="process:ai-wiki-maintainer",
        run="r1",
    )
    assert (res["created"], res["merged"], res["duplicate"]) == (2, 1, 2)

    # First scan populates _ITEM_CACHE; returned items from list_items/next_item must not mutate cache.
    items = M._items(bundle)
    assert len(items) == 2
    listed = M.list_items(bundle, limit=10)["items"]
    listed[0]["status"] = "caller_mutated"
    assert all(i["status"] == "ready" for i in M._items(bundle))

    claimed = M.next_item(bundle, principal="process:ai-wiki-maintainer", run="r1")
    assert claimed["item"]["topic_key"] == "topic:a"
    assert claimed["item"]["status"] == "in_progress"
    assert claimed["ready"] == 1

    # Resuming returns a fresh copy; mutating it does not touch cache.
    resumed = M.next_item(bundle, principal="process:ai-wiki-maintainer", run="r1")
    assert resumed["resumed"] is True
    resumed["item"]["brief"] = "mutated"
    assert M.next_item(bundle, principal="process:ai-wiki-maintainer", run="r1")["item"]["brief"] != "mutated"

    # Releasing lease interrupts in-progress item cleanly without corrupting cache.
    rel = M.release_lease(bundle, "maintainer", principal="process:ai-wiki-maintainer", run="r1")
    assert rel["released"] is True and len(rel["interrupted"]) == 1

    # Corrupting an item.json externally is immediately detected as corrupt (never cached).
    monkeypatch.setattr(M, "_RACY_NS", 2_000_000_000)
    b_id = next(i["id"] for i in M._items(bundle) if i["topic_key"] == "topic:b")
    (bundle / ".okf" / "maint" / "items" / b_id / "item.json").write_text("{bad json", encoding="utf-8")
    good, corrupt = M._scan(bundle)
    assert b_id in corrupt and len(good) == 1
