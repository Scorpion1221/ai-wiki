"""The curation worker drains its queue strictly one job at a time (serialization is what
makes concurrent ingests safe), in FIFO order, and survives a failing job."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from aiwiki.service import worker


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
