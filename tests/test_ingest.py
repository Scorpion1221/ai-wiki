"""Ingest: any file type stored verbatim (raw bytes, original extension, no frontmatter),
curatable types queued, others flagged needs-conversion; the inbox sweep picks up
out-of-band drops. Curation itself is stubbed (no claude)."""
from __future__ import annotations

import base64
import importlib
import shutil
from pathlib import Path

import pytest

from aiwiki.service import ingest as I

AUTH = {"Authorization": "Bearer testtok"}


def _client(bundle: Path, monkeypatch, curate: str = "off"):
    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", curate)
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return appmod, TestClient(appmod.app)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    b = tmp_path / "kb"
    (b / "sources" / "inbox").mkdir(parents=True)
    (b / "index.md").write_text("# kb\n", encoding="utf-8")
    return b


def test_write_source_is_verbatim(bundle: Path) -> None:
    rel, sha = I.write_source(bundle, b"%PDF-1.4\x00\xff raw", "Report Q3.pdf", title="t")
    p = bundle / rel
    assert rel.endswith(".pdf") and p.read_bytes() == b"%PDF-1.4\x00\xff raw"  # bytes untouched
    assert "report-q3" in rel and sha in rel  # slugified name + full content-sha suffix, ext preserved
    # pasted text remains byte-identical but is not named like an OKF concept
    rel2, _ = I.write_source(bundle, b"just text", None, title="note")
    assert rel2.endswith(".md.source") and (bundle / rel2).read_text() == "just text"


def test_write_source_no_samename_collision(bundle: Path) -> None:
    # two different docs sharing a filename get distinct paths (sha suffix) — no overwrite
    a, _ = I.write_source(bundle, b"alpha content", "report.pdf")
    b, _ = I.write_source(bundle, b"beta content", "report.pdf")
    assert a != b
    assert (bundle / a).read_bytes() == b"alpha content"
    assert (bundle / b).read_bytes() == b"beta content"
    # an exact re-upload is idempotent (same path, same bytes)
    a2, _ = I.write_source(bundle, b"alpha content", "report.pdf")
    assert a2 == a


def test_write_source_same_sha_prefix_does_not_collide(bundle: Path, monkeypatch) -> None:
    digests = {
        b"first bytes": "deadbeef" + "0" * 56,
        b"second bytes": "deadbeef" + "1" * 56,
    }

    class FakeHash:
        def __init__(self, data: bytes):
            self.data = data

        def hexdigest(self) -> str:
            return digests[self.data]

    monkeypatch.setattr(I.hashlib, "sha256", FakeHash)

    first, first_sha = I.write_source(bundle, b"first bytes", "report.pdf")
    second, second_sha = I.write_source(bundle, b"second bytes", "report.pdf")

    assert first_sha[:8] == second_sha[:8] == "deadbeef"
    assert first_sha != second_sha
    assert first != second
    assert first_sha in first and second_sha in second
    assert (bundle / first).read_bytes() == b"first bytes"
    assert (bundle / second).read_bytes() == b"second bytes"


def test_is_curatable() -> None:
    assert I.is_curatable("a.md", b"# hi")              # text
    assert I.is_curatable("a.csv", "héllo".encode())    # utf-8 text
    assert I.is_curatable("a.pdf", b"\x00\x01\x02")     # pdf by extension
    assert I.is_curatable("a.png", b"\x89PNG\r\n")      # image by extension
    assert not I.is_curatable("a.bin", b"\x00\x01\xff\xfe")  # opaque binary → not curatable


def test_ingest_text_and_binary_and_unsupported(bundle: Path, monkeypatch) -> None:
    appmod, c = _client(bundle, monkeypatch)
    # pasted text
    r = c.post("/ingest", json={"text": "hello world", "title": "n"}, headers=AUTH).json()
    assert r["source"].endswith(".md.source") and r["curation"] == "off"  # curate off in this deploy
    # a PDF (base64) → curatable, stored verbatim
    pdf = base64.b64encode(b"%PDF-1.4 data").decode()
    r = c.post("/ingest", json={"content_b64": pdf, "filename": "x.pdf"}, headers=AUTH).json()
    assert r["status"] == "queued" and (bundle / r["source"]).read_bytes() == b"%PDF-1.4 data"
    # an opaque binary → stored but flagged needs-conversion
    blob = base64.b64encode(b"\x00\x01\xff\xfe\x00").decode()
    r = c.post("/ingest", json={"content_b64": blob, "filename": "x.bin"}, headers=AUTH).json()
    assert r["status"] == "needs-conversion" and r["curation"] == "needs-conversion"
    # bad base64 → 400
    assert c.post("/ingest", json={"content_b64": "!!notb64!!", "filename": "x"}, headers=AUTH).status_code == 400
    # neither field → 400
    assert c.post("/ingest", json={"title": "nothing"}, headers=AUTH).status_code == 400


def test_ingest_same_content_is_noop_and_failed_job_can_retry(bundle: Path, monkeypatch) -> None:
    _appmod, c = _client(bundle, monkeypatch)
    first = c.post("/ingest", json={"text": "same source", "title": "n"}, headers=AUTH).json()
    second = c.post("/ingest", json={"text": "same source", "title": "renamed"}, headers=AUTH).json()
    assert first["deduplicated"] is False and second["deduplicated"] is True
    assert second["id"] == first["id"]
    assert I.read_job(bundle, first["id"])["curation"] == "off"  # response state is persisted

    failed = I.read_job(bundle, first["id"])
    failed["status"] = "failed"
    I.save_job(bundle, failed)
    retry = c.post("/ingest", json={"text": "same source", "title": "retry"}, headers=AUTH).json()
    assert retry["deduplicated"] is False and retry["id"] != first["id"]


def test_done_source_noop_does_not_recreate_inbox_file(bundle: Path, monkeypatch) -> None:
    _appmod, c = _client(bundle, monkeypatch)
    first = c.post("/ingest", json={"text": "already curated"}, headers=AUTH).json()
    source = bundle / first["source"]
    source.unlink()  # the curator normally moves it from inbox into sources/
    done = I.read_job(bundle, first["id"])
    done["status"] = "done"
    I.save_job(bundle, done)

    again = c.post("/ingest", json={"text": "already curated"}, headers=AUTH).json()
    assert again["deduplicated"] is True and again["id"] == first["id"]
    assert not source.exists()


def test_duplicate_queued_source_is_submitted_once(bundle: Path, monkeypatch) -> None:
    appmod, c = _client(bundle, monkeypatch, curate="auto")
    submitted = []
    monkeypatch.setattr(appmod.worker, "ensure_started", lambda: None)
    monkeypatch.setattr(appmod.worker, "submit", lambda *args: submitted.append(args))

    first = c.post("/ingest", json={"text": "queue once"}, headers=AUTH).json()
    second = c.post("/ingest", json={"text": "queue once"}, headers=AUTH).json()
    assert first["deduplicated"] is False and second["deduplicated"] is True
    assert len(submitted) == 1


def test_receive_save_and_submit_are_one_serialized_lifecycle(bundle: Path, monkeypatch) -> None:
    appmod, c = _client(bundle, monkeypatch, curate="auto")
    original_receive = appmod.I.receive_source
    events = []
    monkeypatch.setattr(appmod.worker, "ensure_started", lambda: None)

    def receive(*args, **kwargs):
        assert appmod.worker._lifecycle_lock.locked()
        events.append("receive")
        return original_receive(*args, **kwargs)

    def submit(*_args):
        assert appmod.worker._lifecycle_lock.locked()
        events.append("submit")

    monkeypatch.setattr(appmod.I, "receive_source", receive)
    monkeypatch.setattr(appmod.worker, "submit", submit)

    response = c.post("/ingest", json={"text": "atomic source"}, headers=AUTH)

    assert response.status_code == 200 and events == ["receive", "submit"]


def test_multiple_ingests_queue_while_worker_mutation_is_running(bundle: Path, monkeypatch) -> None:
    appmod, c = _client(bundle, monkeypatch, curate="auto")
    submitted = []
    monkeypatch.setattr(appmod.worker, "ensure_started", lambda: None)
    monkeypatch.setattr(appmod.worker, "submit", lambda *_args: submitted.append(True))

    with appmod.worker.serialized_mutation():
        first = c.post("/ingest", json={"text": "first queued"}, headers=AUTH)
        second = c.post("/ingest", json={"text": "second queued"}, headers=AUTH)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "queued"
    assert submitted == [True, True]


def test_sweep_picks_up_out_of_band_drops(bundle: Path, monkeypatch) -> None:
    from aiwiki.service import worker
    submitted = []
    monkeypatch.setattr(worker, "submit", lambda b, src, jp: submitted.append(src))
    # drop two files straight into the inbox (as if via scp/git), one curatable, one not
    (bundle / "sources" / "inbox" / "dropped.md").write_text("from scp", encoding="utf-8")
    (bundle / "sources" / "inbox" / "blob.bin").write_bytes(b"\x00\xff\xfe")
    n = worker.sweep_once([bundle])
    assert n == 1 and submitted == ["sources/inbox/dropped.md"]  # only the curatable one queued
    # a second sweep is a no-op — both now have jobs (deduped by sha)
    assert worker.sweep_once([bundle]) == 0


def test_sweep_ignores_inbox_symlink(bundle: Path, monkeypatch, tmp_path: Path) -> None:
    from aiwiki.service import worker

    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = bundle / "sources" / "inbox" / "escape.md"
    link.symlink_to(outside)
    submitted = []
    monkeypatch.setattr(worker, "submit", lambda *args: submitted.append(args))

    assert worker.sweep_once([bundle]) == 0
    assert submitted == []
    assert outside.read_text(encoding="utf-8") == "secret"


def test_sweep_and_delete_share_lifecycle_lock(bundle: Path, monkeypatch) -> None:
    from aiwiki.service import worker

    dropped = bundle / "sources" / "inbox" / "late.md"
    dropped.write_text("late", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(worker, "submit", lambda *args: submitted.append(args))

    with worker.serialized_lifecycle():
        # Simulate the delete critical section without recursively invoking sweep.
        shutil.rmtree(bundle)

    assert not bundle.exists() and submitted == []
    assert worker.sweep_once([bundle]) == 0
    assert not bundle.exists()
