"""Ingest: any file type stored verbatim (raw bytes, original extension, no frontmatter),
curatable types queued, others flagged needs-conversion; the inbox sweep picks up
out-of-band drops. Curation itself is stubbed (no claude)."""
from __future__ import annotations

import base64
import importlib
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
    assert "report-q3" in rel and sha[:8] in rel  # slugified name + content-sha suffix, ext preserved
    # pasted text (no filename) lands as .md, with NO injected frontmatter
    rel2, _ = I.write_source(bundle, b"just text", None, title="note")
    assert rel2.endswith(".md") and (bundle / rel2).read_text() == "just text"


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
    assert r["source"].endswith(".md") and r["curation"] == "off"  # curate off in this deploy
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