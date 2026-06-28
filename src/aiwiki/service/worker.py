"""Serial curation worker.

Ingest only *queues* work; a single background thread drains the queue one job at a
time. Serializing curation is what makes many concurrent writers safe: two curation
passes never touch the bundle or its git tree at once, so the only contention left is
between this worker and *other* writers' pushes — which curate.py handles by rebasing.

On startup, queued jobs left by a previous run are re-enqueued and orphaned "running"
jobs (interrupted by a restart) are marked failed.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from ..runtime import curate
from . import ingest as I

_q: queue.Queue = queue.Queue()
_started = False
_lock = threading.Lock()
SWEEP_INTERVAL_S = 60


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


def _known_shas(bundle: Path) -> set[str]:
    """sha256 of every source any job already tracks (so the sweep never double-enqueues)."""
    shas: set[str] = set()
    jdir = bundle / ".okf" / "jobs"
    if not jdir.is_dir():
        return shas
    for jf in jdir.glob("*.json"):
        try:
            s = json.loads(jf.read_text(encoding="utf-8")).get("sha256")
        except (OSError, ValueError):
            continue
        if s:
            shas.add(s)
    return shas


def sweep_once(bundles: list[Path]) -> int:
    """Pick up sources sitting in sources/inbox/ that no job has seen yet (e.g. dropped
    out-of-band) and queue the curatable ones. Deduped by content sha. Returns #queued."""
    queued = 0
    for b in bundles:
        inbox = b / "sources" / "inbox"
        if not inbox.is_dir():
            continue
        known = _known_shas(b)
        for f in sorted(inbox.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if sha in known:
                continue
            source_rel = f.relative_to(b).as_posix()
            curatable = I.is_curatable(source_rel, data)
            job = I.new_job(b, source_rel, sha, curatable, filename=f.name)
            known.add(sha)
            if curatable:
                submit(b, source_rel, I.job_path(b, job["id"]))
                queued += 1
    return queued


def start_sweeper(bundles_fn) -> None:
    """Run sweep_once on a timer, in the background. bundles_fn() yields current bundle paths."""
    def _loop():
        while True:
            time.sleep(SWEEP_INTERVAL_S)
            try:
                sweep_once(bundles_fn())
            except Exception:  # noqa: BLE001 — a bad sweep must never kill the loop
                pass
    threading.Thread(target=_loop, name="inbox-sweeper", daemon=True).start()


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
