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
        self.mode = "ok"  # ok | fail | slow | short | wrong-dims | null-index | list-body | truncated | trickle
        self.block: threading.Event | None = None  # set: requests wait on it (a build held mid-way)
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
                if outer.block is not None:
                    outer.block.wait(5)
                data = [{"index": i, "embedding": _vector(t)} for i, t in enumerate(body["input"])]
                if outer.mode == "short":
                    data = data[:-1]
                if outer.mode == "wrong-dims":
                    data = [{"index": d["index"], "embedding": d["embedding"][:2]} for d in data]
                if outer.mode == "null-index":
                    data = [{"index": None, "embedding": d["embedding"]} for d in data]
                answer = {"data": data}
                if outer.mode == "list-body":
                    answer = data
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if outer.mode == "truncated":
                    self.send_header("Content-Length", str(len(raw) + 100))
                    self.end_headers()
                    self.wfile.write(raw[: len(raw) // 2])
                    return
                if outer.mode == "trickle":
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    for byte in raw[:20]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.2)
                    return
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


def _builder_idle(root: Path, deadline: float = 5.0) -> None:
    """Wait until the store's builder thread (and its closing compaction) has finished."""
    end = time.monotonic() + deadline
    while V._stores[root].building or any(t.name == "embedding-builder" for t in threading.enumerate()):
        assert time.monotonic() < end, "the embedding builder never finished"
        time.sleep(0.01)


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
    assert rows[0]["snippet"] == "" and rows[0]["description"]  # the description explains it once


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


@pytest.mark.parametrize("mode", ["fail", "slow", "short", "wrong-dims", "null-index", "list-body", "truncated"])
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
    _builder_idle(root)  # the compaction that ends the build rewrites the file: tear it afterwards
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


def test_the_query_timeout_bounds_the_whole_call(tmp_path, embedder) -> None:
    """A server trickling one byte at a time would hold a per-read socket timeout forever."""
    root = _bundle(tmp_path / "kb")
    _ready(root)
    embedder.mode = "trickle"

    started = time.monotonic()
    _rows, _total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert time.monotonic() - started < 1.5  # AIWIKI_EMBED_TIMEOUT_S=0.5 here; 20 bytes take 4 s
    assert retrieval["mode"] == "lexical" and "no answer within" in retrieval["reason"]


def test_after_a_failed_query_embedding_searches_stay_lexical_without_waiting(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    embedder.mode = "slow"
    B.hybrid_search_page(root, "needle", 10)
    embedder.requests.clear()

    started = time.monotonic()
    _rows, _total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert time.monotonic() - started < 0.2 and embedder.requests == []
    assert retrieval["mode"] == "lexical" and retrieval["reason"].startswith("query embedding paused")


def test_a_repeated_query_reuses_its_vector_and_stays_hybrid_while_the_endpoint_fails(tmp_path, embedder) -> None:
    """2026-11-05: for ten hours 37% of the mirror's query embeddings timed out and those searches came back
    lexical, although agents rerun the same searches. A query embedded once is not sent again."""
    root = _bundle(tmp_path / "kb")
    _ready(root)
    embedder.requests.clear()
    first = B.hybrid_search_page(root, "how long does the classroom take to start", 10)
    assert first[2]["mode"] == "hybrid" and len(embedder.requests) == 1

    second = B.hybrid_search_page(root, "how long does the classroom take to start", 10)
    assert len(embedder.requests) == 1 and second[:2] == first[:2] and second[2]["mode"] == "hybrid"

    embedder.mode = "fail"
    failed = B.hybrid_search_page(root, "pricing experiment", 10)  # a new query: embedded, fails, pauses
    assert failed[2]["mode"] == "lexical" and failed[2]["reason"].startswith("query embedding failed")
    paused = B.hybrid_search_page(root, "how long does the classroom take to start", 10)
    assert paused[:2] == first[:2] and paused[2]["mode"] == "hybrid"       # a cached one, during the pause
    assert len(embedder.requests) == 2


def test_the_query_vector_cache_is_bounded_least_recently_used(tmp_path, embedder, monkeypatch) -> None:
    monkeypatch.setattr(V, "QUERY_CACHE", 3)
    root = _bundle(tmp_path / "kb")
    _ready(root)
    for q in ("one", "two", "three"):
        B.hybrid_search_page(root, q, 5)
    B.hybrid_search_page(root, "one", 5)                     # "one" is now the most recent
    B.hybrid_search_page(root, "four", 5)                    # evicts "two", the least recent
    store = V._stores[root]
    assert list(store.queries) == [V.query_input(q) for q in ("three", "one", "four")]
    embedder.requests.clear()
    B.hybrid_search_page(root, "two", 5)
    assert embedder.requests == [[V.query_input("two")]]


def test_a_failed_query_embedding_is_not_cached(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    embedder.mode = "wrong-dims"
    assert B.hybrid_search_page(root, "needle", 10)[2]["mode"] == "lexical"
    assert V.query_input("needle") not in V._stores[root].queries
    V._stores[root].paused_until = 0.0                       # past the pause
    embedder.mode = "ok"
    assert B.hybrid_search_page(root, "needle", 10)[2]["mode"] == "hybrid"


def test_another_model_never_reuses_a_cached_query_vector(tmp_path, embedder, monkeypatch) -> None:
    """The cache lives in the store, and a change of model, dimension or endpoint makes a new store."""
    root = _bundle(tmp_path / "kb")
    _ready(root)
    B.hybrid_search_page(root, "needle", 10)
    old = V._stores[root]
    monkeypatch.setenv("AIWIKI_EMBED_MODEL", "fixture-model-2")
    assert V.store(root) is not old and not V.store(root).queries


def test_a_pull_during_the_build_never_embeds_a_chunk_twice(tmp_path, embedder, monkeypatch) -> None:
    monkeypatch.setattr(V, "BATCH", 1)
    root = _bundle(tmp_path / "kb")
    embedder.block = threading.Event()
    B.hybrid_search_page(root, "warm up", 5)  # the builder starts and waits inside its first request
    time.sleep(0.1)
    edited = root / "features" / "tutor-init.md"
    edited.write_text(edited.read_text(encoding="utf-8").replace("8 秒", "12 秒"), encoding="utf-8")
    time.sleep(0.15)
    B.hybrid_search_page(root, "warm up", 5)  # the new snapshot replaces the index mid-build
    embedder.block.set()
    _ready(root)

    embedded = [text for batch in embedder.requests for text in batch if not text.startswith("task:")]
    assert len(embedded) == len(set(embedded))
    assert len(embedded) <= 4  # three concepts, plus at most the one old version already in flight


def test_an_unwritable_cache_keeps_vectors_in_memory(tmp_path, embedder, monkeypatch) -> None:
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("AIWIKI_EMBED_CACHE", str(blocked / "cache"))
    V._stores.clear()
    root = _bundle(tmp_path / "kb")
    try:
        _ready(root)
        rows, _total, retrieval = B.hybrid_search_page(root, "how long does the classroom take to start", 5)
    finally:
        blocked.chmod(0o700)

    assert retrieval["mode"] == "hybrid" and rows[0]["path"] == "features/tutor-init.md"
    assert "error" in B.embedding_state(root)
    assert sum(len(b) for b in embedder.requests if not b[0].startswith("task:")) == 3


def test_the_cache_keeps_only_current_chunks(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    edited = root / "features" / "tutor-init.md"
    for n in range(3):
        edited.write_text(edited.read_text(encoding="utf-8").replace(f"{8 + n} 秒", f"{9 + n} 秒"), encoding="utf-8")
        time.sleep(0.15)
        _ready(root)
    _builder_idle(root)  # compaction runs when the builder finishes

    cache = next((tmp_path / "embed-cache").glob("*.jsonl"))
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 3


def test_total_counts_every_lexical_match_whatever_the_embedder_does(tmp_path, embedder) -> None:
    root = _bundle(tmp_path / "kb")
    _ready(root)
    lexical_total = B._lexical_search_page(root, "定价", None)[1]

    hybrid = B.hybrid_search_page(root, "定价", 10)
    embedder.mode = "fail"
    V._stores[root].paused_until = 0
    V._stores[root].queries.clear()                          # else the same query reuses its vector
    fallback = B.hybrid_search_page(root, "定价", 10)

    assert hybrid[2]["mode"] == "hybrid" and fallback[2]["mode"] == "lexical"
    assert hybrid[1] >= lexical_total and fallback[1] == lexical_total


@pytest.mark.parametrize("name,value", [("AIWIKI_EMBED_DIMS", "many"), ("AIWIKI_EMBED_TIMEOUT_S", "soon"),
                                        ("AIWIKI_EMBED_TIMEOUT_S", "0")])
def test_a_malformed_setting_keeps_search_lexical(tmp_path, embedder, monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    root = _bundle(tmp_path / "kb")

    rows, total, retrieval = B.hybrid_search_page(root, "needle", 10)

    assert retrieval is None and B.embedding_state(root) is None
    assert (rows, total) == B._lexical_search_page(root, "needle", 10)


def test_the_key_is_not_in_the_settings_repr(embedder) -> None:
    assert "fixture-key" not in repr(V.settings())


def test_search_and_health_endpoints(tmp_path, embedder, monkeypatch) -> None:
    from test_smoke import AUTH, _client

    root = _bundle(tmp_path / "kb")
    client = _client(root, monkeypatch)
    health = client.get("/health", headers=AUTH).json()  # starts the build
    assert health["embedding"]["model"] == "fixture-model"
    end = time.monotonic() + 5
    while not client.get("/health", headers=AUTH).json()["embedding"]["ready"]:
        assert time.monotonic() < end
        time.sleep(0.02)

    answer = client.get("/search", params={"q": "how long does the classroom take to start"}, headers=AUTH).json()

    assert answer["retrieval"]["mode"] == "hybrid"
    assert answer["results"][0]["path"] == "features/tutor-init.md" and answer["results"][0]["retrieval"] == "vector"
    for name in ("AIWIKI_EMBED_URL", "AIWIKI_EMBED_MODEL", "AIWIKI_EMBED_KEY"):
        monkeypatch.delenv(name)
    off = _client(root, monkeypatch)
    assert "embedding" not in off.get("/health", headers=AUTH).json()
    assert set(off.get("/search", params={"q": "needle"}, headers=AUTH).json()) == {"results", "total"}


def test_a_new_index_trims_once_its_matrix_is_built(tmp_path, embedder, monkeypatch) -> None:
    """Each pull builds a slightly larger matrix than the one it frees; the freed one goes back to the system."""
    from types import SimpleNamespace

    trims = []
    monkeypatch.setattr(V, "_malloc", SimpleNamespace(trim=lambda: trims.append(1)))
    monkeypatch.setattr(B, "_malloc", SimpleNamespace(trim=lambda: None))  # rescans trim on their own
    root = _bundle(tmp_path / "kb")
    time.sleep(0.15)  # past the racy window: one snapshot, so one index
    _ready(root)
    B.hybrid_search_page(root, "classroom", 5)
    B.hybrid_search_page(root, "classroom", 5)
    assert len(trims) == 1                                   # the matrix is built once per index


def test_trim_runs_where_glibc_has_it_and_is_harmless_elsewhere() -> None:
    import platform

    from aiwiki.service import malloc

    if platform.libc_ver()[0] == "glibc":
        assert malloc._malloc_trim is not None                # the read mirror's image: the symbol resolves
    malloc.trim()


def test_a_pull_is_embedded_before_the_next_search_asks(tmp_path, embedder, monkeypatch) -> None:
    """2026-11-05: the first search after each production pull was answered lexically ("chunk vectors are being
    built"), because only a request started the build. The service's warmer starts it on its own."""
    from test_smoke import AUTH, _client

    root = _bundle(tmp_path / "kb")
    client = _client(root, monkeypatch)
    from aiwiki.service import app as appmod

    monkeypatch.setattr(appmod, "EMBED_WARM_S", 0.05, raising=False)
    with client:
        client.get("/health", headers=AUTH)                 # the mirror's first request registers the bundle
        end = time.monotonic() + 5
        while not V._stores[root].state()["ready"]:
            assert time.monotonic() < end
            time.sleep(0.02)
        chunks = V._stores[root].state()["chunks"]
        edited = root / "features" / "tutor-init.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "\n\n" + "pulled paragraph " * 120 + "\n",
                          encoding="utf-8")
        end = time.monotonic() + 5                           # no request from here on: only the warmer looks
        while not (V._stores[root].state()["chunks"] > chunks and V._stores[root].state()["ready"]):
            assert time.monotonic() < end, V._stores[root].state()
            time.sleep(0.02)
        answer = client.get("/search", params={"q": "how long does the classroom take to start"}, headers=AUTH)
    assert answer.json()["retrieval"]["mode"] == "hybrid"


def _warmed(root: Path, before, deadline: float = 5.0):
    """Wait until the cached snapshot is a settled one other than ``before`` with its search index
    and link graph built; return it."""
    end = time.monotonic() + deadline
    while True:
        snap = B._snapshots.get(root)
        if (snap is not None and snap is not before and snap.settled
                and B._SearchIndex in snap.derived and B._LinkGraph in snap.derived):
            return snap
        assert time.monotonic() < end, None if snap is None else (snap is before, snap.settled, list(snap.derived))
        time.sleep(0.02)


@pytest.mark.parametrize("endpoint", ["ok", "fail"])
def test_a_pull_has_its_search_index_and_link_graph_before_the_next_read(tmp_path, embedder, monkeypatch,
                                                                          endpoint) -> None:
    """2026-11-06: in production the first search after each pull took ~2.8 s, the next one ~0.35 s: the
    warmer embedded the pull but left the search index (~2 s) and link graph (~1.3 s) to the first request.
    The warmer builds them too, also while the embedder is down and its builder has given up."""
    from test_smoke import AUTH, _client

    monkeypatch.setattr(V.time, "sleep", lambda _s: None)    # the builder's backoff, not the test's clock
    root = _bundle(tmp_path / "kb")
    embedder.mode = endpoint
    client = _client(root, monkeypatch)                     # sets the token the app module reads on import
    from aiwiki.service import app as appmod

    monkeypatch.setattr(appmod, "EMBED_WARM_S", 0.02, raising=False)
    with client:
        client.get("/health", headers=AUTH)                 # the mirror's first request registers the bundle
        first = _warmed(root, None)
        edited = root / "features" / "tutor-init.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "\n\nA pulled paragraph about latency.\n",
                          encoding="utf-8")
        pulled = _warmed(root, first)                       # no request from here on: only the warmer looks
        built = {cls: pulled.derived[cls] for cls in (B._SearchIndex, B._LinkGraph)}
        assert client.get("/search", params={"q": "pulled paragraph latency"}, headers=AUTH).json()["results"]
        assert client.get("/links", params={"path": "features/tutor-init.md"}, headers=AUTH).status_code == 200
        _builder_idle(root)                                 # the search may have restarted it: end it here
    assert B._snapshots[root] is pulled                    # the reads used the warmed snapshot ...
    assert all(pulled.derived[cls] is index for cls, index in built.items())  # ... and its indexes as built


def test_the_warmer_leaves_a_builder_that_gave_up_to_the_next_search(tmp_path, embedder, monkeypatch) -> None:
    """While the endpoint is down the warmer does not restart the builder every pass; a search still does."""
    from test_smoke import AUTH, _client

    from aiwiki.service import app as appmod

    monkeypatch.setattr(V.time, "sleep", lambda _s: None)    # the builder's backoff, not the test's clock
    root = _bundle(tmp_path / "kb")
    embedder.mode = "fail"
    client = _client(root, monkeypatch)
    monkeypatch.setattr(appmod, "EMBED_WARM_S", 0.02, raising=False)
    with client:
        client.get("/health", headers=AUTH)
        end = time.monotonic() + 5
        while not V.gave_up(root):
            assert time.monotonic() < end
            time.sleep(0.01)
        _builder_idle(root)
        asked = len(embedder.requests)
        time.sleep(0.2)                                      # ten warmer passes
        assert len(embedder.requests) == asked
        client.get("/search", params={"q": "classroom"}, headers=AUTH)
        end = time.monotonic() + 5
        while len(embedder.requests) == asked:              # a search restarts the build
            assert time.monotonic() < end
            time.sleep(0.01)


def test_rank_matches_a_reference_scan_and_builds_the_matrix_once(tmp_path, embedder) -> None:
    """The matrix product ranks exactly like scoring each chunk in Python, ties broken by path,
    and concurrent first searches build the matrix once."""
    import concurrent.futures as cf
    import random

    rng = random.Random(7)
    index = V._Index()
    for n in range(60):
        path = f"c/{n:02d}.md"
        for _ in range(rng.randint(1, 4)):
            vec = [rng.choice([0.0, 1.0, rng.random()]) for _ in range(DIMS)]  # ties included
            index.owners.append(path)
            index.shas.append(f"{len(index.owners)}")
            index.vectors[len(index.owners) - 1] = V._unit(vec) or V._unit([1.0] * DIMS)
    query = V._unit([0.3, 0.1, 0.9, 0.2])
    best = {}
    for i, owner in enumerate(index.owners):
        sim = sum(a * b for a, b in zip(index.vectors[i], query, strict=True))
        best[owner] = max(best.get(owner, -2.0), sim)
    reference = sorted(best, key=lambda p: (-round(best[p], 5), p))[:V.CANDIDATES]

    builds = []
    real_stack = V.np.stack

    def counted_stack(*args, **kwargs):
        builds.append(1)
        time.sleep(0.05)  # a slow build, so concurrent first searches overlap it
        return real_stack(*args, **kwargs)

    store = V.Store(V.settings(), tmp_path)
    original = V.embed
    V.embed = lambda config, inputs, timeout: [query]
    V.np.stack = counted_stack
    start = threading.Barrier(8)

    def first_search(_):
        start.wait()
        return store.rank(index, "q")[0]

    try:
        with cf.ThreadPoolExecutor(8) as ex:
            results = list(ex.map(first_search, range(8)))
    finally:
        V.embed, V.np.stack = original, real_stack

    assert len(builds) == 1
    assert all(r == results[0] for r in results)
    ranked = results[0]
    assert sorted(ranked) == sorted(reference)
    assert all(round(best[a], 5) >= round(best[b], 5) for a, b in zip(ranked, ranked[1:], strict=False))
