"""Hybrid search: lexical ranking fused with an OpenAI-compatible embedding endpoint.

A fake endpoint embeds by keyword buckets, so an English query can land on a Chinese concept
the way a multilingual model does, and every failure mode is scripted.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aiwiki.service import bundle as B
from aiwiki.service import vectors as V

DIMS = 4
# Each axis stands for a meaning; the fake model maps words of either language onto it.
AXES = [("手机", "mobile", "流量", "traffic"), ("课堂", "classroom", "tutor", "初始化"),
        ("定价", "pricing", "price"), ("needle",)]


def _vector(text: str) -> list[float]:
    low = text.lower()
    return [float(sum(low.count(w) for w in words)) + 0.01 for words in AXES]


class _Embedder:
    def __init__(self):
        self.mode = "ok"  # ok | fail | slow | short | wrong-dims
        self.requests: list[list[str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/v1/embeddings"
                assert self.headers["Authorization"] == "Bearer fixture-key"
                outer.requests.append(body["input"])
                if outer.mode == "fail":
                    self.send_response(503)
                    self.end_headers()
                    return
                if outer.mode == "slow":
                    time.sleep(1.0)
                data = [{"index": i, "embedding": _vector(t)} for i, t in enumerate(body["input"])]
                if outer.mode == "short":
                    data = data[:-1]
                if outer.mode == "wrong-dims":
                    data = [{"index": d["index"], "embedding": d["embedding"][:2]} for d in data]
                raw = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


@pytest.fixture
def embedder(tmp_path, monkeypatch):
    e = _Embedder()
    monkeypatch.setenv("AIWIKI_EMBED_URL", e.url)
    monkeypatch.setenv("AIWIKI_EMBED_MODEL", "fixture-model")
    monkeypatch.setenv("AIWIKI_EMBED_KEY", "fixture-key")
    monkeypatch.setenv("AIWIKI_EMBED_DIMS", str(DIMS))
    monkeypatch.setenv("AIWIKI_EMBED_TIMEOUT_S", "0.5")
    monkeypatch.setenv("AIWIKI_EMBED_CACHE", str(tmp_path / "embed-cache"))
    V._stores.clear()
    yield e
    e.server.shutdown()
    V._stores.clear()


def _bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# Test bundle\n', encoding="utf-8")
    concepts = {
        "metrics/mobile-share.md": ("Writing 工具页手机端流量占比", "六个工具页的手机端访问占比。",
                                    "手机端 流量 占比 较高。"),
        "features/tutor-init.md": ("Tutor 课堂初始化耗时", "课堂初始化要等多久。", "课堂 初始化 平均 8 秒。"),
        "experiments/pricing-v2.md": ("本地化定价 V2", "五国本地化定价实验。", "定价 实验 needle。"),
    }
    for rel, (title, desc, body) in concepts.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"""---
type: Reference
title: {title}
description: {desc}
tags: [test]
status: stable
generated: {{by: process:test, at: 2026-08-13T10:00:00Z}}
stale_after: 2099-01-01
sources:
  - {{id: s-{path.stem}, resource: /sources/{path.stem}.md.source}}
---

# Summary

{body}
""", encoding="utf-8")
    return root


def _ready(root: Path, deadline: float = 5.0) -> None:
    """Search once to start the builder, then wait until every chunk is embedded."""
    B.hybrid_search_page(root, "warm up", 5)
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if (B.embedding_state(root) or {}).get("ready"):
            return
        time.sleep(0.02)
    raise AssertionError(f"embedding never finished: {B.embedding_state(root)}")


def test_without_embedding_settings_search_is_the_lexical_search(tmp_path, monkeypatch) -> None:
    for name in ("AIWIKI_EMBED_URL", "AIWIKI_EMBED_MODEL", "AIWIKI_EMBED_KEY"):
        monkeypatch.delenv(name, raising=False)
    root = _bundle(tmp_path / "kb")

    rows, total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert retrieval is None and B.embedding_state(root) is None
    assert (rows, total) == B._lexical_search_page(root, "needle", 10) == B.search_page(root, "needle", 10)
    assert all("retrieval" not in row for row in rows)


def test_an_english_question_finds_the_chinese_concept(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)

    query = "how long does the classroom take to start"  # no word of it is in the Chinese text
    assert B._lexical_search_page(root, query, 5)[0] == []

    rows, _total, retrieval = B.hybrid_search_page(root, query, 5)

    assert retrieval["mode"] == "hybrid" and retrieval["embedding"]["ready"] is True
    assert rows[0]["path"] == "features/tutor-init.md"
    assert rows[0]["retrieval"] == "vector" and rows[0]["vector_rank"] == 1 and rows[0]["score"] is None
    assert rows[0]["match"] == {"phrase": False, "coverage": 0.0, "fields": [], "terms": []}


def test_rows_found_both_ways_keep_their_lexical_score_and_terms(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)

    rows, _total, _retrieval = B.hybrid_search_page(root, "手机端 流量", 5)

    top = rows[0]
    assert top["path"] == "metrics/mobile-share.md" and top["retrieval"] == "both"
    assert isinstance(top["score"], float) and top["match"]["terms"] and top["vector_rank"] == 1


def test_an_exact_phrase_hit_stays_first(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    lexical = B._lexical_search_page(root, "定价 实验", 10)[0]
    assert lexical[0]["match"]["phrase"]

    rows = B.hybrid_search_page(root, "定价 实验", 10)[0]

    assert rows[0]["path"] == lexical[0]["path"]


@pytest.mark.parametrize("mode", ["fail", "slow", "short", "wrong-dims"])
def test_a_failing_query_embedding_falls_back_to_lexical(tmp_path, embedder, mode) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    embedder.mode = mode

    rows, total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert retrieval["mode"] == "lexical" and retrieval["reason"].startswith("query embedding failed")
    lexical, lexical_total = B._lexical_search_page(root, "needle", 10)
    assert [r["path"] for r in rows] == [r["path"] for r in lexical] and total == lexical_total
    assert all(r["retrieval"] == "lexical" for r in rows)


def test_a_search_never_waits_for_chunk_embedding(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    embedder.mode = "slow"

    started = time.monotonic()
    rows, _total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert time.monotonic() - started < 0.5
    assert retrieval["mode"] == "lexical" and "being built" in retrieval["reason"]
    assert [r["path"] for r in rows] == [r["path"] for r in B._lexical_search_page(root, "needle", 10)[0]]


def test_chunk_vectors_are_cached_on_disk_and_only_changes_are_embedded(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    assert sum(len(batch) for batch in embedder.requests) == 3  # three one-chunk concepts; the warm-up
    # query came before they were ready, so it was answered lexically and never embedded

    V._stores.clear()  # a restart: a new store reads the cache back
    embedder.requests.clear()
    _ready(root)
    assert sum(len(b) for b in embedder.requests if not b[0].startswith("task:")) == 0

    (root / "features" / "tutor-init.md").write_text(
        (root / "features" / "tutor-init.md").read_text(encoding="utf-8").replace("8 秒", "12 秒"), encoding="utf-8")
    embedder.requests.clear()
    time.sleep(0.15)  # past the snapshot's racy window
    _ready(root)
    chunk_batches = [b for b in embedder.requests if not b[0].startswith("task:")]
    assert sum(map(len, chunk_batches)) == 1 and "12 秒" in chunk_batches[0][0]


def test_a_torn_cache_line_is_embedded_again(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    cache = next((tmp_path / "embed-cache").glob("*.jsonl"))
    lines = cache.read_text(encoding="utf-8").splitlines()
    cache.write_text("\n".join(lines[:-1] + [lines[-1][:20]]) + "\n", encoding="utf-8")

    V._stores.clear()
    embedder.requests.clear()
    _ready(root)

    assert sum(len(b) for b in embedder.requests if not b[0].startswith("task:")) == 1


def test_health_reports_the_embedding_state(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)

    state = B.embedding_state(root)

    assert state == {"enabled": True, "model": "fixture-model", "dims": DIMS, "chunks": 3, "embedded": 3,
                     "ready": True}


def test_chunks_pack_paragraphs_and_drop_footnote_definitions() -> None:
    body = "# Summary\n\n" + "\n\n".join(f"第{i}段 " + "字" * 500 for i in range(6)) + "\n\n[^src]: a source note\n"

    parts = V.chunks("标题", "描述", body)

    assert all(p.startswith("title: 标题 | text: ") for p in parts)
    assert parts[0].removeprefix("title: 标题 | text: ").startswith("描述")
    assert all(len(p.split(" | text: ", 1)[1]) <= V.CHUNK_CHARS for p in parts) and len(parts) >= 2
    assert not any("a source note" in p for p in parts)


def test_fusion_weights_follow_the_query_language() -> None:
    assert V.weights("手机端 流量") == (1.0, 1.0)
    assert V.weights("mobile traffic share") == (1.0, 3.0)
    lexical, vector = ["a", "b", "c"], ["c", "b", "a"]
    assert V.fuse(lexical, set(), vector, "english words")[0] == "c"  # meaning leads in English
    assert V.fuse(lexical, {"b"}, vector, "english words")[0] == "b"  # a phrase hit stays first
