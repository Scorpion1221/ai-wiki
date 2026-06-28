"""Serial curation worker.

Ingest only *queues* work; a single background thread drains the queue one job at a
time. Serializing curation is what makes many concurrent writers safe: two curation
passes never touch the bundle or its git tree at once, so the only contention left is
between this worker and *other* writers' pushes — which curate.py handles by rebasing.

On startup, queued jobs left by a previous run are re-enqueued and orphaned "running"
jobs (interrupted by a restart) are marked failed.
"""
from __future__ import annotations

import json
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..runtime import curate

_q: queue.Queue = queue.Queue()
_started = False
_lock = threading.Lock()


def submit(bundle: Path, source_rel: str, job_path: Path) -> None:
    _q.put((bundle, source_rel, job_path))


def _run() -> None:
    while True:
        bundle, source_rel, job_path = _q.get()
        try:
            curate.run(bundle, source_rel, job_path)
        except Exception:  # noqa: BLE001 — curate.run records its own failures; never kill the worker
            pass
        finally:
            _q.task_done()


def ensure_started() -> None:
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_run, name="curation-worker", daemon=True).start()
        _started = True


def recover(bundles: list[Path]) -> None:
    """Re-enqueue jobs left 'queued' by a prior run; fail jobs left 'running' (interrupted)."""
    for b in bundles:
        jdir = b / ".okf" / "jobs"
        if not jdir.is_dir():
            continue
        for jf in sorted(jdir.glob("*.json")):
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            status, source = job.get("status"), job.get("source")
            if status == "queued" and source:
                submit(b, source, jf)
            elif status == "running":
                job["status"] = "failed"
                job["error"] = "interrupted by service restart"
                job["finished"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                jf.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
