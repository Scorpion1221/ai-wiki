"""The read mirror answers from the snapshot its warmer last prepared (bundle.warm), so a read right
after a pull never builds the next snapshot's scan, indexes or chunk vectors. On 0.3.27 a search
waited 4.8-8.3 s for ~20 s after each production pull while the warmer built them (2026-11-13)."""
from __future__ import annotations

import threading
import time

import pytest
from test_service_vectors import _builder_idle, _bundle

from aiwiki.service import bundle as B
from aiwiki.service import vectors as V

pytest_plugins = ["test_service_vectors"]  # its embedder fixture

QUESTION = "how long does the classroom take to start"


class _Builds:
    """Counts every scan and every index or matrix built while it is installed."""

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        real_scan = B._scan
        monkeypatch.setattr(B, "_scan", lambda *a, **k: (self.calls.append("scan"), real_scan(*a, **k))[1])
        for cls in (B._SearchIndex, B._LinkGraph, B._Listing):
            real = cls.__init__
            monkeypatch.setattr(cls, "__init__", self._spy(cls.__name__, real))
        real_matrix = V._Index.ready_matrix

        def matrix(index):
            if index.matrix is None:
                self.calls.append("matrix")
            return real_matrix(index)

        monkeypatch.setattr(V._Index, "ready_matrix", matrix)
        real_ensure = V.Store.ensure
        monkeypatch.setattr(V.Store, "ensure", lambda *a, **k: (self.calls.append("ensure"), real_ensure(*a, **k))[1])

    def _spy(self, name, real):
        def init(obj, *a, **k):
            self.calls.append(name)
            real(obj, *a, **k)
        return init


def _hold_chunk_embedding(monkeypatch) -> threading.Event:
    """Hold the builder's chunk requests until the returned event is set; query embeddings go
    through, as in production, where a search's query and a pull's chunks are separate requests."""
    release = threading.Event()
    real = V.embed

    def embed(config, inputs, timeout):
        if not inputs[0].startswith("task:"):
            release.wait(10)
        return real(config, inputs, timeout)

    monkeypatch.setattr(V, "embed", embed)
    return release


def _prepared(root, embedder):
    """A bundle the warmer has prepared once: every chunk embedded, the snapshot handed to reads."""
    time.sleep(0.15)                                    # past the racy window of the files _bundle wrote
    B.embedding_state(root)                             # the mirror's first read registers the store
    end = time.monotonic() + 5
    while not V._stores[root].state()["ready"]:
        assert time.monotonic() < end, V._stores[root].state()
        time.sleep(0.02)
    _builder_idle(root)
    B.warm(root)
    view = B._prepared[root]
    assert view.snapshot is B._snapshots[root] and view.index.matrix is not None
    return view


def _pull(root):
    """What a production pull does to one concept: a new paragraph and a new generation."""
    edited = root / "features" / "tutor-init.md"
    text = edited.read_text(encoding="utf-8")
    edited.write_text(text.replace("at: 2026-08-13T10:00:00Z", "at: 2026-11-13T10:00:00Z")
                      + "\n\nA moonbeam paragraph the pull added.\n", encoding="utf-8")
    time.sleep(0.15)                                    # settled, as the warmer's next pass sees it
    return edited


def test_reads_during_preparation_answer_from_the_prepared_snapshot_and_build_nothing(tmp_path, embedder,
                                                                                       monkeypatch) -> None:
    root = _bundle(tmp_path / "kb")
    before = _prepared(root, embedder)
    edited = _pull(root)
    release = _hold_chunk_embedding(monkeypatch)        # the new chunk's embedding is still in flight
    B.warm(root)                                        # scans and indexes the pull, then waits for it
    newest = V._stores[root].index
    assert B._prepared[root] is before and newest is not before.index and newest.pending

    builds = _Builds(monkeypatch)
    rows, _total, retrieval = B.hybrid_search_page(root, QUESTION, 5)
    assert retrieval["mode"] == "hybrid" and rows[0]["path"] == "features/tutor-init.md"
    assert B.grep(root, "moonbeam") == []               # the previous snapshot: the pull is not served yet
    assert B.links(root, "features/tutor-init.md")["path"] == "features/tutor-init.md"
    assert B.list_dir(root, "features")
    assert B.health(root)["concepts"] == 3
    assert B.embedding_state(root)["ready"] is True
    meta = B.metadata_for_path(root, edited, edited.read_text(encoding="utf-8"))
    assert meta["generated_at"].startswith("2026-11-13")  # cat's metadata describes the text cat serves
    assert builds.calls == []
    assert V._stores[root].index is newest              # no read moved the builder back to the old snapshot

    release.set()
    _builder_idle(root)
    B.warm(root)                                        # the chunks are in: the pull is handed over
    after = B._prepared[root]
    assert after.snapshot is B._snapshots[root] and after.index is newest and newest.matrix is not None
    builds.calls.clear()
    assert [hit["path"] for hit in B.grep(root, "moonbeam")] == ["features/tutor-init.md"]
    rows, _total, retrieval = B.hybrid_search_page(root, "moonbeam paragraph", 5)
    assert retrieval["mode"] == "hybrid" and rows[0]["path"] == "features/tutor-init.md"
    B.links(root, "features/tutor-init.md")
    B.list_dir(root, "features", recursive=True)
    assert builds.calls == []                           # the warmer built all of it before handing it over


def test_a_pass_with_nothing_changed_keeps_the_prepared_snapshot(tmp_path, embedder, monkeypatch) -> None:
    root = _bundle(tmp_path / "kb")
    view = _prepared(root, embedder)
    builds = _Builds(monkeypatch)
    B.warm(root)
    assert B._prepared[root] is view and builds.calls == []


def test_a_change_the_warmer_does_not_prepare_in_time_is_read_as_before(tmp_path, embedder, monkeypatch) -> None:
    """PREPARE_MAX_S bounds how long reads keep the previous snapshot after the warmer found a change."""
    monkeypatch.setattr(B, "PREPARE_MAX_S", 0.3)
    root = _bundle(tmp_path / "kb")
    _prepared(root, embedder)
    _pull(root)
    release = _hold_chunk_embedding(monkeypatch)
    try:
        B.warm(root)                                    # finds the change, cannot hand it over yet
        assert B.grep(root, "moonbeam") == []
        time.sleep(0.35)
        assert B.grep(root, "moonbeam")                 # past the bound: the read scans for itself
        _rows, _total, retrieval = B.hybrid_search_page(root, QUESTION, 5)
        assert retrieval["mode"] == "lexical" and "being built" in retrieval["reason"]  # as on 0.3.27
    finally:
        release.set()
    _builder_idle(root)


def test_without_a_recent_warmer_pass_a_read_scans_as_before(tmp_path, embedder, monkeypatch) -> None:
    """A warmer that stopped looking (WARMER_STALE_S) never pins reads to an old snapshot."""
    monkeypatch.setattr(B, "WARMER_STALE_S", 0.3)
    root = _bundle(tmp_path / "kb")
    _prepared(root, embedder)
    _pull(root)                                         # no warmer pass from here on
    assert B.grep(root, "moonbeam") == []               # a pass 0.15 s ago: still the prepared snapshot
    time.sleep(0.2)
    assert B.grep(root, "moonbeam")
    _builder_idle(root)


def test_a_writer_mutation_is_read_at_once(tmp_path, embedder) -> None:
    """The writer drops its snapshots after each mutation (drop_snapshots): the next read rescans."""
    root = _bundle(tmp_path / "kb")
    _prepared(root, embedder)
    _pull(root)
    B.drop_snapshots()
    assert root not in B._prepared
    assert B.grep(root, "moonbeam")
    _builder_idle(root)


def test_with_the_builder_given_up_the_pull_is_handed_over_and_a_search_retries(tmp_path, embedder,
                                                                              monkeypatch) -> None:
    """While the endpoint is down the warmer hands a pull over without its vectors (searches answer
    lexically, as on 0.3.27), and a search still restarts the builder."""
    root = _bundle(tmp_path / "kb")
    _prepared(root, embedder)
    _pull(root)
    monkeypatch.setattr(V.time, "sleep", lambda _s: None)  # the builder's backoff (time.sleep for this test too)
    embedder.mode = "fail"
    B.warm(root)                                        # starts the builder (restart=False only skips a gave-up one)
    end = time.monotonic() + 5
    while not V.gave_up(root):
        assert time.monotonic() < end
        time.sleep(0.01)
    _builder_idle(root)
    B.warm(root)                                        # given up: hand the pull over without its vectors
    view = B._prepared[root]
    assert view.snapshot is B._snapshots[root] and view.index.pending
    assert B.grep(root, "moonbeam")
    asked = len(embedder.requests)
    _rows, _total, retrieval = B.hybrid_search_page(root, QUESTION, 5)
    assert retrieval["mode"] == "lexical" and "being built" in retrieval["reason"]
    end = time.monotonic() + 5
    while len(embedder.requests) == asked:              # the search restarted the builder
        assert time.monotonic() < end
        time.sleep(0.01)
    embedder.mode = "ok"
    end = time.monotonic() + 5
    while view.index.pending:
        assert time.monotonic() < end
        time.sleep(0.02)
    _builder_idle(root)
    B.warm(root)                                        # the restarted build finished: the matrix is built here
    assert view.index.matrix is not None
    assert B.hybrid_search_page(root, QUESTION, 5)[2]["mode"] == "hybrid"


@pytest.mark.parametrize("endpoint", ["ok"])
def test_the_service_warmer_hands_a_pull_over_before_a_read_sees_it(tmp_path, embedder, monkeypatch,
                                                                    endpoint) -> None:
    """End to end through the app: the warmer thread prepares a pull and the next /search reads it."""
    from test_smoke import AUTH, _client

    root = _bundle(tmp_path / "kb")
    client = _client(root, monkeypatch)
    from aiwiki.service import app as appmod

    monkeypatch.setattr(appmod, "EMBED_WARM_S", 0.02, raising=False)
    with client:
        time.sleep(0.15)
        client.get("/health", headers=AUTH)
        end = time.monotonic() + 5
        while root not in B._prepared:
            assert time.monotonic() < end
            time.sleep(0.02)
        first = B._prepared[root]
        _pull(root)
        end = time.monotonic() + 5
        while B._prepared[root] is first:                # no request from here on: only the warmer looks
            assert time.monotonic() < end
            time.sleep(0.02)
        builds = _Builds(monkeypatch)
        answer = client.get("/search", params={"q": "moonbeam paragraph"}, headers=AUTH).json()
        assert answer["retrieval"]["mode"] == "hybrid" and answer["results"][0]["path"] == "features/tutor-init.md"
        assert builds.calls == []
        _builder_idle(root)
