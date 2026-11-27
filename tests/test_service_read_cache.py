"""The read path's per-bundle snapshot: reused while the bundle's file stats hold, rebuilt on any
change a read could see, and never a way to serve what a cold read would refuse."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_service_bundle_v02 import _concept

from aiwiki.service import bundle as B
from aiwiki.service import worker

SENTINEL = "P0_CACHE_SENTINEL"


@pytest.fixture(autouse=True)
def _fresh_cache():
    B.drop_snapshots()
    yield
    B.drop_snapshots()


@pytest.fixture
def settled(monkeypatch):
    """Every write so far is older than the racy window, so a scan is reusable."""
    clock = SimpleNamespace(time_ns=lambda: time.time_ns() + 10**9, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)


def _paths(root: Path) -> list[str]:
    return [rel for _path, rel in B.concepts(root)]


def _rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_the_snapshot_is_reused_until_the_bundle_changes(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")

    first = B._snapshot(tmp_path)
    assert B._snapshot(tmp_path) is first
    assert [record[1] for record in first.records] == _paths(tmp_path) == ["topics/one.md", "topics/two.md"]
    assert list(B._concept_records(tmp_path)) == list(first.records)
    assert B.search(tmp_path, "needle", top_k=None) == B.search(tmp_path, "needle", top_k=None)
    assert B._snapshot(tmp_path) is first


def test_a_rescan_hands_the_replaced_snapshot_back_to_the_system(tmp_path: Path, settled, monkeypatch) -> None:
    """2026-11-05: the 0.3.16 mirror grew 150 -> 290 MiB in 12 h of pulls; freed scans stayed in glibc's heaps.
    A rescan trims once the old snapshot is released; a reused snapshot trims nothing."""
    import weakref

    trims = []
    _concept(tmp_path, "one")
    old = weakref.ref(B._snapshot(tmp_path))
    monkeypatch.setattr(B, "_malloc", SimpleNamespace(trim=lambda: trims.append(old() is None)))
    B._snapshot(tmp_path)
    assert trims == []
    _rewrite(tmp_path / "topics" / "one.md", "needle", "needle edited")
    B._snapshot(tmp_path)
    assert trims == [True]                                   # trimmed after the replaced snapshot was freed
    _rewrite(tmp_path / "topics" / "one.md", "edited", "edited again")
    monkeypatch.setattr(B, "time", SimpleNamespace(time_ns=time.time_ns, monotonic_ns=time.monotonic_ns))
    B._snapshot(tmp_path)                                    # the pull replaces a settled snapshot: one trim ...
    assert len(trims) == 2
    B._snapshot(tmp_path)                                    # ... its racy rescans replace an unsettled one: none
    B._snapshot(tmp_path)
    assert len(trims) == 2


def test_an_edit_is_seen_even_when_size_and_mtime_are_restored(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    path = tmp_path / "topics" / "one.md"
    _rewrite(path, "needle", "needle aaaa")
    before = path.stat()
    assert B.grep(tmp_path, "aaaa") and B._snapshots[tmp_path].settled

    time.sleep(0.05)  # past any filesystem's timestamp tick
    _rewrite(path, "aaaa", "bbbb")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)

    assert B.grep(tmp_path, "aaaa") == []
    assert [hit["text"] for hit in B.grep(tmp_path, "bbbb")] == ["needle bbbb"]


def test_a_scan_right_after_a_write_is_not_reused(tmp_path: Path, monkeypatch) -> None:
    """A second write in the same timestamp tick can leave every stat as it was, so a scan this
    close to the newest change never stands in for a later request's own scan."""
    _concept(tmp_path, "one")
    path = tmp_path / "topics" / "one.md"
    newest = path.stat().st_ctime_ns
    clock = SimpleNamespace(time_ns=lambda: newest + 1_000_000, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)

    assert _paths(tmp_path) == ["topics/one.md"]
    assert not B._snapshots[tmp_path].settled
    _rewrite(path, "needle", "bbbbbb")
    assert B.grep(tmp_path, "needle") == [] and B.grep(tmp_path, "bbbbbb")


def test_whole_second_timestamps_widen_the_racy_window(tmp_path: Path, monkeypatch) -> None:
    """HFS+ and ext3 keep whole seconds (FAT two), so a same-size rewrite within a second of a scan can leave
    every stat as it was: a scan in that second, past the usual window, is still not reused."""
    _concept(tmp_path, "one")
    path = tmp_path / "topics" / "one.md"
    second = path.stat().st_ctime_ns // 10**9 * 10**9
    real_fingerprint = B._fingerprint

    def coarse(root):  # the stats such a filesystem shows for writes all within one second
        top, entries = real_fingerprint(root)
        return top, tuple((*entry[:4], second, second) for entry in entries)

    monkeypatch.setattr(B, "_fingerprint", coarse)
    clock = SimpleNamespace(time_ns=lambda: second + 900_000_000, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)
    assert B.grep(tmp_path, "needle") and not B._snapshots[tmp_path].settled
    _rewrite(path, "needle", "zzqqzz")
    assert B.grep(tmp_path, "needle") == [] and B.grep(tmp_path, "zzqqzz")


def test_added_removed_and_renamed_concepts_are_seen(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    assert _paths(tmp_path) == ["topics/one.md"]

    _concept(tmp_path, "two")
    assert _paths(tmp_path) == ["topics/one.md", "topics/two.md"]
    assert B.health(tmp_path)["concepts"] == 2

    (tmp_path / "topics" / "one.md").unlink()
    assert _paths(tmp_path) == ["topics/two.md"]
    assert [hit["path"] for hit in B.search(tmp_path, "needle")] == ["topics/two.md"]

    (tmp_path / "topics" / "two.md").rename(tmp_path / "topics" / "three.md")
    assert _paths(tmp_path) == ["topics/three.md"]
    (tmp_path / "topics").rename(tmp_path / "areas")
    assert _paths(tmp_path) == ["areas/three.md"]
    assert B.count_concepts(tmp_path) == 1


def test_a_concept_or_directory_swapped_for_a_symlink_disappears(tmp_path: Path, settled) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    _concept(bundle, "one")
    _concept(bundle, "two")
    _concept(outside, SENTINEL.lower())
    assert _paths(bundle) == ["topics/one.md", "topics/two.md"]

    one = bundle / "topics" / "one.md"
    one.unlink()
    one.symlink_to(outside / "topics" / f"{SENTINEL.lower()}.md")
    assert _paths(bundle) == ["topics/two.md"]
    assert B.grep(bundle, SENTINEL) == [] and B.search(bundle, SENTINEL) == []

    (bundle / "topics").rename(tmp_path / "moved")
    (bundle / "topics").symlink_to(tmp_path / "moved", target_is_directory=True)
    assert _paths(bundle) == [] and B.health(bundle)["concepts"] == 0

    (bundle / "index.md").rename(tmp_path / "index.md")
    (bundle / "index.md").symlink_to(tmp_path / "index.md")
    assert B.health(bundle)["okf_version"] == ""


def test_private_paths_never_appear_and_never_invalidate(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "valid")
    snapshot = B._snapshot(tmp_path)
    for private in (".git", "sources", "sources/inbox", ".okf", "topics/.git"):
        (tmp_path / private).mkdir(parents=True, exist_ok=True)
        _concept(tmp_path / private, SENTINEL.lower())

    assert B._snapshot(tmp_path) is snapshot
    B.drop_snapshots()
    assert _paths(tmp_path) == ["topics/valid.md"]
    assert B.grep(tmp_path, SENTINEL) == [] and B.search(tmp_path, SENTINEL) == []


@pytest.mark.parametrize("area", [".hidden", "area/sources"])
def test_an_edit_under_a_hidden_or_nested_sources_directory_is_seen(tmp_path: Path, settled, area: str) -> None:
    """Only a top-level ``sources`` is never a concept: a hidden directory and a ``sources`` deeper down hold
    concepts search and grep serve, so the fingerprint watches them."""
    (tmp_path / area).mkdir(parents=True)
    _concept(tmp_path / area, "one")
    _concept(tmp_path, "two")
    rel = f"{area}/topics/one.md"
    assert sorted(hit["path"] for hit in B.search(tmp_path, "needle", top_k=None)) == sorted([rel, "topics/two.md"])

    _rewrite(tmp_path / rel, "needle", "zzqq")
    assert [hit["path"] for hit in B.search(tmp_path, "zzqq")] == [rel]
    assert [hit["path"] for hit in B.grep(tmp_path, "zzqq")] == [rel]


def test_an_index_meta_edit_is_seen_by_ls(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    meta = tmp_path / "index-meta.yaml"
    meta.write_text("directories:\n  topics: before\n", encoding="utf-8")

    def description() -> str:
        return next(item["description"] for item in B.list_dir(tmp_path) if item["path"] == "topics/")

    assert description() == "before"
    meta.write_text("directories:\n  topics: after the edit\n", encoding="utf-8")
    assert description() == "after the edit"


def test_a_writer_mutation_drops_the_snapshot(tmp_path: Path, settled, monkeypatch) -> None:
    """Even a change no file stat shows is read once the mutation that made it ends."""
    _concept(tmp_path, "one")
    assert B._snapshot(tmp_path) is B._snapshot(tmp_path)
    monkeypatch.setattr(B, "_fingerprint", lambda root: ("blind", ()))
    assert B.grep(tmp_path, "needle")

    with worker.serialized_mutation():
        _rewrite(tmp_path / "topics" / "one.md", "needle", "bbbbbb")
        assert B.grep(tmp_path, "bbbbbb") == []  # stats are blind here; readers wait in the service
    assert tmp_path not in B._snapshots
    assert [hit["text"] for hit in B.grep(tmp_path, "bbbbbb")] == ["bbbbbb"]


def test_a_scan_overlapping_a_writer_mutation_is_not_kept(tmp_path: Path, settled, monkeypatch) -> None:
    _concept(tmp_path, "one")
    real_scan = B._scan

    def scan_while_a_writer_finishes(*args):
        snapshot = real_scan(*args)
        B.drop_snapshots()  # what serialized_mutation does as it ends
        return snapshot

    monkeypatch.setattr(B, "_scan", scan_while_a_writer_finishes)
    assert _paths(tmp_path) == ["topics/one.md"]
    assert tmp_path not in B._snapshots


def test_concurrent_readers_during_a_rebuild_all_get_the_whole_bundle(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(40):
        _concept(tmp_path, f"c{n:02d}")
    assert len(_paths(tmp_path)) == 40
    _concept(tmp_path, "c40")
    expected = [f"topics/c{n:02d}.md" for n in range(41)]

    scans = []
    real_scan, real_profile = B._scan, B._profile_document

    def counted_scan(root, *args):
        scans.append(root)
        return real_scan(root, *args)

    def slow_profile(root, path):
        time.sleep(0.002)  # hold the rebuild open while the other readers arrive
        return real_profile(root, path)

    monkeypatch.setattr(B, "_scan", counted_scan)
    monkeypatch.setattr(B, "_profile_document", slow_profile)
    start = threading.Barrier(8)
    seen: list[list[str]] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            start.wait()
            seen.append(_paths(tmp_path))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [] and len(seen) == 8
    assert all(paths == expected for paths in seen)
    assert scans == [tmp_path]  # one rebuild, the other seven waited for it


def test_readers_queued_behind_a_racy_rescan_share_it(tmp_path: Path, monkeypatch) -> None:
    """Right after a change no scan is reusable on its stats alone, so readers that arrived before a scan
    began wait for it and take its result instead of each rescanning in turn."""
    for n in range(10):
        _concept(tmp_path, f"c{n}")
    newest = max(path.stat().st_ctime_ns for path in (tmp_path / "topics").iterdir())
    arrivals = threading.Semaphore(0)

    def monotonic_ns() -> int:
        arrivals.release()
        return time.monotonic_ns()

    monkeypatch.setattr(B, "time", SimpleNamespace(time_ns=lambda: newest + 1_000_000, monotonic_ns=monotonic_ns))
    scans = []
    real_scan = B._scan
    lock = B._snapshot_locks.setdefault(tmp_path, threading.Lock())

    def counted_scan(root, *args):
        scans.append(root)
        return real_scan(root, *args)

    monkeypatch.setattr(B, "_scan", counted_scan)
    seen: list[list[str]] = []
    with lock:  # a scan in progress: every reader below arrives before the next one begins
        threads = [threading.Thread(target=lambda: seen.append(_paths(tmp_path))) for _ in range(6)]
        for thread in threads:
            thread.start()
        assert all(arrivals.acquire(timeout=60) for _ in threads)
    for thread in threads:
        thread.join(timeout=60)

    assert seen == [[f"topics/c{n}.md" for n in range(10)]] * 6
    assert scans == [tmp_path]


def test_search_counts_every_match_but_snippets_only_the_rows_it_returns(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(5):
        _concept(tmp_path, f"c{n}", mentions=n + 1)
    everything = B.search(tmp_path, "needle", top_k=None)
    assert len(everything) == 5

    snippets = []
    real_snippet = B._snippet

    def counted_snippet(body, *args):
        snippets.append(body)
        return real_snippet(body, *args)

    monkeypatch.setattr(B, "_snippet", counted_snippet)
    rows, total = B.search_page(tmp_path, "needle", 2)
    assert (rows, total) == (everything[:2], 5)
    assert len(snippets) == 2

    from test_smoke import AUTH, _client

    snippets.clear()
    response = _client(tmp_path, monkeypatch).get("/search", params={"q": "needle", "top_k": 2}, headers=AUTH).json()
    assert response == {"results": everything[:2], "total": 5}
    assert len(snippets) == 2


def test_search_after_an_edit_returns_the_edited_result(tmp_path: Path, settled) -> None:
    """A term's postings are kept per snapshot, so a query run before an edit never answers after it."""
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")
    before = B.search(tmp_path, "needle", top_k=None)
    assert [hit["path"] for hit in before] == ["topics/one.md", "topics/two.md"]
    assert B.search(tmp_path, "haystack") == []

    _rewrite(tmp_path / "topics" / "two.md", "needle", "needle needle needle haystack")
    after = B.search(tmp_path, "needle", top_k=None)
    assert [hit["path"] for hit in after] == ["topics/two.md", "topics/one.md"]
    assert after[0]["score"] > before[1]["score"]
    assert [hit["path"] for hit in B.search(tmp_path, "haystack")] == ["topics/two.md"]

    (tmp_path / "topics" / "two.md").unlink()
    assert [hit["path"] for hit in B.search(tmp_path, "needle", top_k=None)] == ["topics/one.md"]
    assert B.search(tmp_path, "haystack") == []


def test_the_postings_memo_is_bounded(tmp_path: Path, settled, monkeypatch) -> None:
    _concept(tmp_path, "one")
    monkeypatch.setattr(B, "_POSTINGS_MEMO", 2)
    first = B.search(tmp_path, "needle summary test")
    index = B._derived(B._snapshot(tmp_path), B._SearchIndex)
    assert list(index.memo) == ["summary", "test"]
    assert B.search(tmp_path, "needle summary test") == first


def test_a_long_term_is_scored_but_never_memoized(tmp_path: Path, settled) -> None:
    """No word is that long, and a query may hold any number of them: the memo's bytes stay bounded."""
    _concept(tmp_path, "one")
    long = "n" * (B._MEMO_TERM_CHARS + 1)
    _rewrite(tmp_path / "topics" / "one.md", "needle", f"needle {long}")
    hits = B.search(tmp_path, f"needle {long}")
    assert [hit["path"] for hit in hits] == ["topics/one.md"] and hits[0]["match"]["terms"] == ["needle", long]
    assert B.search(tmp_path, f"needle {long}") == hits
    for n in range(3):
        assert B.search(tmp_path, f"{n}{long}") == []
    assert list(B._derived(B._snapshot(tmp_path), B._SearchIndex).memo) == ["needle"]


def _graph(root: Path, rel: str) -> tuple[list[str], list[str]]:
    graph = B.links(root, rel)
    return [item["path"] for item in graph["outbound"]], [item["path"] for item in graph["inbound"]]


def test_links_follow_a_link_edit_in_both_directions(tmp_path: Path, settled) -> None:
    for name in ("one", "two", "three"):
        _concept(tmp_path, name)
    one = tmp_path / "topics" / "one.md"
    _rewrite(one, "needle", "[Two](two.md)")
    assert _graph(tmp_path, "topics/one.md") == (["topics/two.md"], [])
    assert _graph(tmp_path, "topics/two.md") == ([], ["topics/one.md"])

    _rewrite(one, "[Two](two.md)", "[Three](../topics/three.md)")
    assert _graph(tmp_path, "topics/one.md") == (["topics/three.md"], [])
    assert _graph(tmp_path, "topics/two.md") == ([], [])
    assert _graph(tmp_path, "topics/three.md") == ([], ["topics/one.md"])

    _rewrite(tmp_path / "topics" / "two.md", "needle", "[Three](/topics/three.md)")
    assert _graph(tmp_path, "topics/three.md") == ([], ["topics/one.md", "topics/two.md"])
    one.unlink()
    assert _graph(tmp_path, "topics/three.md") == ([], ["topics/two.md"])


def test_lexical_links_are_resolved_once_per_snapshot(tmp_path: Path, settled, monkeypatch) -> None:
    for name in ("one", "two", "three"):
        _concept(tmp_path, name)
    _rewrite(tmp_path / "topics" / "one.md", "needle", "[Two](two.md) [Three](/topics/three.md)")
    resolved = []
    real_outbound = B._outbound

    def counted_outbound(root, body, rel, ids):
        resolved.append(rel)
        return real_outbound(root, body, rel, ids)

    monkeypatch.setattr(B, "_outbound", counted_outbound)
    assert _graph(tmp_path, "topics/two.md") == ([], ["topics/one.md"])
    assert sorted(resolved) == ["topics/one.md", "topics/three.md", "topics/two.md"]
    resolved.clear()
    assert _graph(tmp_path, "topics/one.md") == (["topics/two.md", "topics/three.md"], [])
    assert _graph(tmp_path, "topics/three.md") == ([], ["topics/one.md"])
    assert resolved == []


def test_a_link_that_is_not_lexical_is_resolved_per_request(tmp_path: Path, settled) -> None:
    """``..`` after a name can climb out of a symlink in a tree the fingerprint never walks, so
    such a link is resolved on every request, exactly as an uncached read resolves it."""
    for name in ("one", "two"):
        _concept(tmp_path, name)
    (tmp_path / "x" / "y" / "z").mkdir(parents=True)
    _concept(tmp_path / "x", "two")  # x/index.md is structural, never a concept
    (tmp_path / "sources" / "dir").mkdir(parents=True)
    _rewrite(tmp_path / "topics" / "one.md", "needle", "[Two](../sources/dir/../../topics/two.md)")
    assert _graph(tmp_path, "topics/one.md") == (["topics/two.md"], [])
    snapshot = B._snapshot(tmp_path)

    (tmp_path / "sources" / "dir").rmdir()
    (tmp_path / "sources" / "dir").symlink_to(tmp_path / "x" / "y" / "z", target_is_directory=True)
    assert B._snapshot(tmp_path) is snapshot  # nothing under sources/ is watched
    assert _graph(tmp_path, "topics/one.md") == (["x/topics/two.md"], [])
    assert _graph(tmp_path, "x/topics/two.md") == ([], ["topics/one.md"])
    assert _graph(tmp_path, "topics/two.md") == ([], [])


def test_ls_counts_and_kinds_follow_added_and_removed_concepts(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    _concept(tmp_path / "topics", "deep")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "a.md.source").write_text("raw", encoding="utf-8")

    def counts() -> dict:
        return {item["path"]: item["concepts"] for item in B.list_dir(tmp_path) if item["kind"] == "dir"}

    assert counts() == {"sources/": 0, "topics/": 2}
    assert {item["path"]: item["concepts"] for item in B.list_dir(tmp_path, "topics") if item["kind"] == "dir"} == {
        "topics/topics/": 1}
    assert B.count_concepts(tmp_path) == 2

    _concept(tmp_path, "two")
    broken = tmp_path / "topics" / "broken.md"
    broken.write_text("---\ntype: [unterminated\n---\nneedle\n", encoding="utf-8")
    assert counts() == {"sources/": 0, "topics/": 3}
    kinds = {item["path"]: item["kind"] for item in B.list_dir(tmp_path, recursive=True)}
    assert kinds == {
        "index.md": "doc", "sources/a.md.source": "file", "topics/broken.md": "invalid",
        "topics/one.md": "concept", "topics/topics/deep.md": "concept", "topics/two.md": "concept",
        "topics/index.md": "doc",
    }
    assert B.list_dir(tmp_path, "topics/broken.md")[0]["description"] == "invalid strict OKF v0.2 concept"
    assert B.metadata_for_path(tmp_path, broken) is None

    (tmp_path / "topics" / "one.md").unlink()
    _rewrite(tmp_path / "topics" / "two.md", "status: stable", "status: draft")
    assert counts() == {"sources/": 0, "topics/": 2}
    assert B.count_concepts(tmp_path) == 2
    assert "topics/one.md" not in {item["path"] for item in B.list_dir(tmp_path, recursive=True)}
    assert B.metadata_for_path(tmp_path, tmp_path / "topics" / "two.md")["status"] == "draft"
    assert {item["path"]: item["status"] for item in B.list_dir(tmp_path, "topics") if item["kind"] == "concept"} == {
        "topics/two.md": "draft"}

    broken.write_text(broken.read_text(encoding="utf-8").replace("[unterminated", "Reference"), encoding="utf-8")
    assert B.list_dir(tmp_path, "topics/broken.md")[0]["kind"] == "invalid"  # still not a valid concept
    (tmp_path / "topics" / "topics").rename(tmp_path / "topics" / ".hidden")
    assert counts() == {"sources/": 0, "topics/": 2}  # a hidden directory's concepts still count
    assert "topics/.hidden/deep.md" not in {item["path"] for item in B.list_dir(tmp_path, recursive=True)}
    assert "topics/.hidden/deep.md" in {item["path"] for item in B.list_dir(tmp_path, recursive=True, show_all=True)}


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a directory its reader cannot traverse")
def test_ls_recursive_leaves_out_a_file_no_stat_reaches(tmp_path: Path, settled) -> None:
    """A directory that can be listed but not traversed (mode 600) names files no stat reaches: they are
    not shown as regular files, and the request does not fail."""
    _concept(tmp_path, "one")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "notes.txt").write_text("x", encoding="utf-8")
    locked.chmod(0o600)
    try:
        assert [item["path"] for item in B.list_dir(tmp_path, recursive=True)] == ["index.md", "topics/one.md"]
        assert B.list_dir(tmp_path, "locked", recursive=True, show_all=True) == []
    finally:
        locked.chmod(0o755)


def test_a_caller_changing_a_returned_row_never_changes_the_next_read(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    (tmp_path / "index-meta.yaml").write_text("directories:\n  topics: [concepts]\n", encoding="utf-8")

    def concept_row() -> dict:
        return next(item for item in B.list_dir(tmp_path, "topics") if item["kind"] == "concept")

    def topics_row() -> dict:
        return next(item for item in B.list_dir(tmp_path) if item["path"] == "topics/")

    B.search(tmp_path, "needle")[0]["tags"].append("caller")
    concept_row()["tags"].append("caller")
    topics_row()["description"].append("caller")
    assert B.search(tmp_path, "needle")[0]["tags"] == concept_row()["tags"] == ["test"]
    assert topics_row()["description"] == ["concepts"]


def test_grep_follows_an_edit(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")
    path = tmp_path / "topics" / "two.md"
    assert [(hit["path"], hit["text"]) for hit in B.grep(tmp_path, "NEEDLE", fixed=True)] == [
        ("topics/one.md", "needle"), ("topics/two.md", "needle")]

    _rewrite(path, "needle", "needle\nsecond Needle line [x](y.md)")
    assert [(hit["path"], hit["line"]) for hit in B.grep(tmp_path, "needle", fixed=True)] == [
        ("topics/one.md", 15), ("topics/two.md", 15), ("topics/two.md", 16)]
    assert [hit["text"] for hit in B.grep(tmp_path, "](y.md)", fixed=True)] == ["second Needle line [x](y.md)"]
    assert [hit["line"] for hit in B.grep(tmp_path, r"^second \w+", "topics")] == [16]

    path.unlink()
    assert [hit["path"] for hit in B.grep(tmp_path, "needle", fixed=True)] == ["topics/one.md"]
    assert B.grep(tmp_path, "](y.md)", fixed=True) == []


# A rescan carries over the record of every file unchanged since the previous scan (2026-11-27: a pull
# changes a median of 7 of 2,348 concepts, and reparsing all of them held two copies of the bundle while
# reads kept the previous snapshot, 1.8 times the mirror's steady memory at each pull).


def _parsed(monkeypatch) -> list[str]:
    """The concepts each scan reads and parses from disk, in order."""
    parsed: list[str] = []
    real_profile = B._profile_document

    def counted(root, path):
        parsed.append(Path(path).relative_to(root).as_posix())
        return real_profile(root, path)

    monkeypatch.setattr(B, "_profile_document", counted)
    return parsed


def _cold(root: Path):
    """What a read answers from a snapshot scanned with nothing to carry over."""
    B.drop_snapshots()
    return (
        [(hit["path"], hit["score"], hit["snippet"]) for hit in B.search(root, "needle zzqq", top_k=None)],
        B.grep(root, "needle|zzqq"),
        B.list_dir(root, recursive=True),
        _graph(root, "topics/c1.md"),
        [record[2:] for record in B._snapshot(root).records],
    )


def test_a_rescan_parses_only_the_files_that_changed(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(6):
        _concept(tmp_path, f"c{n}")
    first = B._snapshot(tmp_path)
    parsed = _parsed(monkeypatch)

    _rewrite(tmp_path / "topics" / "c2.md", "needle", "zzqq")
    _concept(tmp_path, "c6")
    (tmp_path / "topics" / "c4.md").unlink()
    second = B._snapshot(tmp_path)

    assert sorted(parsed) == ["topics/c2.md", "topics/c6.md"]
    old = {record[1]: record for record in first.records}
    for record in second.records:
        assert (record is old.get(record[1])) == (record[1] not in ("topics/c2.md", "topics/c6.md"))
    assert [record[1] for record in second.records] == [f"topics/c{n}.md" for n in (0, 1, 2, 3, 5, 6)]
    assert [hit["path"] for hit in B.search(tmp_path, "zzqq")] == ["topics/c2.md"]


def test_a_carried_over_concept_is_reread_after_a_restored_stat_edit(tmp_path: Path, settled, monkeypatch) -> None:
    """An edit that keeps the size and restores the mtime still changes the ctime the entry holds."""
    _concept(tmp_path, "c0")
    _concept(tmp_path, "c1")
    path = tmp_path / "topics" / "c1.md"
    _rewrite(path, "needle", "needle aaaa")
    before = path.stat()
    B._snapshot(tmp_path)
    time.sleep(0.05)
    _rewrite(path, "aaaa", "bbbb")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    _rewrite(tmp_path / "topics" / "c0.md", "needle", "zzqq")  # some other file changed too
    parsed = _parsed(monkeypatch)

    assert [hit["text"] for hit in B.grep(tmp_path, "bbbb")] == ["needle bbbb"]
    assert sorted(parsed) == ["topics/c0.md", "topics/c1.md"]


def test_a_file_racy_at_the_previous_scan_is_parsed_again(tmp_path: Path, monkeypatch) -> None:
    """A file written within the racy window of a scan may change again with the same stat, so its
    record is never carried over; a file settled at that scan is, even if the scan as a whole was not."""
    _concept(tmp_path, "old")
    _concept(tmp_path, "new")
    old, new = tmp_path / "topics" / "old.md", tmp_path / "topics" / "new.md"
    os.utime(old, ns=(1, 1))  # (ctime cannot be set; the clock below makes old's settled and new's racy)
    old_ctime, new_ctime = old.stat().st_ctime_ns, new.stat().st_ctime_ns
    real_fingerprint = B._fingerprint

    def pinned(root):  # old.md changed long ago, new.md just now
        top, entries = real_fingerprint(root)
        return top, tuple((*e[:5], old_ctime - 10**10 if e[0] == "topics/old.md" else e[5]) for e in entries)

    monkeypatch.setattr(B, "_fingerprint", pinned)
    clock = SimpleNamespace(time_ns=lambda: new_ctime + 1_000_000, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)
    first = B._snapshot(tmp_path)
    assert not first.settled
    parsed = _parsed(monkeypatch)

    _concept(tmp_path, "third")
    second = B._snapshot(tmp_path)
    assert sorted(parsed) == ["topics/new.md", "topics/third.md"]
    assert next(r for r in second.records if r[1] == "topics/old.md") is next(
        r for r in first.records if r[1] == "topics/old.md")


def test_a_writer_mutation_leaves_nothing_to_carry_over(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(3):
        _concept(tmp_path, f"c{n}")
    B._snapshot(tmp_path)
    parsed = _parsed(monkeypatch)
    with worker.serialized_mutation():
        pass
    B._snapshot(tmp_path)
    assert sorted(parsed) == ["topics/c0.md", "topics/c1.md", "topics/c2.md"]


def test_a_drop_during_a_queued_rescan_is_not_carried_over(tmp_path: Path, settled, monkeypatch) -> None:
    """The snapshot a rescan carries records over from is read together with the generation the scan is
    kept under: a writer's drop just after a reader took the bundle's lock leaves nothing to carry over
    into a scan that is then kept, since the drop says file stats may not show what the writer changed."""
    for n in range(3):
        _concept(tmp_path, f"c{n}")
    B._snapshot(tmp_path)
    _rewrite(tmp_path / "topics" / "c0.md", "needle", "zzqq")  # so the reader rescans

    bundle_lock = B._snapshot_locks.setdefault(tmp_path, threading.Lock())

    class DropAfterTheLockedRead(dict):
        dropped = False

        def get(self, key, default=None):
            value = super().get(key, default)
            # the first read with the bundle's lock held and the guard free: a writer's drop lands here
            if not DropAfterTheLockedRead.dropped and bundle_lock.locked() and not B._snapshot_guard.locked():
                DropAfterTheLockedRead.dropped = True
                B.drop_snapshots()
            return value

    monkeypatch.setattr(B, "_snapshots", DropAfterTheLockedRead(B._snapshots))
    parsed = _parsed(monkeypatch)
    B._snapshot(tmp_path)
    assert DropAfterTheLockedRead.dropped
    assert sorted(parsed) == ["topics/c0.md", "topics/c1.md", "topics/c2.md"]


def test_the_search_index_reuses_the_fields_of_carried_over_concepts(tmp_path: Path, settled, monkeypatch) -> None:
    """While the previous snapshot is still held (the warmer's prepared view keeps it for reads until
    hand-over: the case that held two copies), the new index normalizes only the changed concepts."""
    for n in range(4):
        _concept(tmp_path, f"c{n}")
    B.search(tmp_path, "needle")
    held = B._snapshot(tmp_path)  # noqa: F841 - what the prepared view does
    normalized: list[str] = []
    real_fields = B._search_fields

    def counted(rel, fm, body):
        normalized.append(rel)
        return real_fields(rel, fm, body)

    monkeypatch.setattr(B, "_search_fields", counted)
    _rewrite(tmp_path / "topics" / "c1.md", "needle", "zzqq")
    hits = B.search(tmp_path, "needle zzqq", top_k=None)
    assert normalized == ["topics/c1.md"]
    assert [(hit["path"], hit["score"]) for hit in hits] == [
        (hit["path"], hit["score"]) for hit in (B.drop_snapshots() or B.search(tmp_path, "needle zzqq", top_k=None))]


def test_reads_after_carried_over_rescans_equal_a_cold_scan(tmp_path: Path, settled, monkeypatch) -> None:
    """Search scores (the per-field averages and document frequencies are over the whole new snapshot),
    grep, ls and links after a sequence of pulls answer exactly what a scan from nothing answers. The
    previous snapshot is held while each next one is indexed, as the warmer's prepared view holds it for
    reads, so every step's index reuses the fields of the concepts the pull left alone."""
    for n in range(8):
        _concept(tmp_path, f"c{n}", mentions=n % 3 + 1)
    B.search(tmp_path, "needle")
    normalized: list[str] = []
    real_fields = B._search_fields

    def counted(rel, fm, body):
        normalized.append(rel)
        return real_fields(rel, fm, body)

    steps = [
        lambda: _rewrite(tmp_path / "topics" / "c1.md", "needle", "zzqq [C3](c3.md)"),
        lambda: _concept(tmp_path, "c8", mentions=5),
        lambda: (tmp_path / "topics" / "c5.md").unlink(),
        lambda: (tmp_path / "topics" / "c6.md").rename(tmp_path / "topics" / "c9.md"),
        lambda: _rewrite(tmp_path / "topics" / "c3.md", "needle", "zzqq zzqq [C1](c1.md)"),
    ]
    reindexed = [["topics/c1.md"], ["topics/c8.md"], [], ["topics/c9.md"], ["topics/c3.md"]]
    for step, expected in zip(steps, reindexed, strict=True):
        held = B._snapshot(tmp_path)  # what the prepared view keeps for reads until the hand-over
        step()
        monkeypatch.setattr(B, "_search_fields", counted)
        normalized.clear()
        warm = (
            [(hit["path"], hit["score"], hit["snippet"]) for hit in B.search(tmp_path, "needle zzqq", top_k=None)],
            B.grep(tmp_path, "needle|zzqq"),
            B.list_dir(tmp_path, recursive=True),
            _graph(tmp_path, "topics/c1.md"),
            [record[2:] for record in B._snapshot(tmp_path).records],
        )
        assert normalized == expected  # the index was built from the held one's fields, not cold
        monkeypatch.setattr(B, "_search_fields", real_fields)
        del held
        assert warm == _cold(tmp_path)
        B.search(tmp_path, "needle")  # the next step builds on a carried-over snapshot, as pulls do


def test_a_carried_over_snapshot_never_keeps_its_predecessor_alive(tmp_path: Path, settled) -> None:
    """The second index is built from the first one's fields while the first snapshot is held (the
    prepared view); once that hold ends, neither the first snapshot nor its index stays alive."""
    import gc
    import weakref

    _concept(tmp_path, "c0")
    _concept(tmp_path, "c1")
    B.search(tmp_path, "needle")
    held = B._snapshot(tmp_path)
    first, first_index = weakref.ref(held), weakref.ref(held.derived[B._SearchIndex])
    _rewrite(tmp_path / "topics" / "c1.md", "needle", "zzqq")
    B.search(tmp_path, "needle")
    second = B._snapshot(tmp_path)
    assert second is not held and second.basis() is held and B._SearchIndex in second.derived
    del held
    gc.collect()
    assert first() is None and first_index() is None


def test_an_index_after_a_racy_rescan_still_reuses_the_held_snapshots_fields(tmp_path: Path, monkeypatch) -> None:
    """A warmer pass within the racy window of a pull scans without indexing (warm returns early) and
    the next pass rescans: that settled snapshot's index reuses the fields of the snapshot reads hold."""
    for n in range(4):
        _concept(tmp_path, f"c{n}")
    later = SimpleNamespace(time_ns=lambda: time.time_ns() + 10**9, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", later)
    B.search(tmp_path, "needle")
    held = B._snapshot(tmp_path)  # the prepared view
    time.sleep(0.15)  # the pull comes later than the racy window after the files it leaves alone
    _rewrite(tmp_path / "topics" / "c2.md", "needle", "zzqq")
    changed_at = (tmp_path / "topics" / "c2.md").stat().st_ctime_ns
    racy_clock = SimpleNamespace(time_ns=lambda: changed_at + 1_000_000, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", racy_clock)
    racy = B._snapshot(tmp_path)
    assert not racy.settled and B._SearchIndex not in racy.derived
    _rewrite(tmp_path / "topics" / "c0.md", "needle", "needle aaaa")  # the next pass finds the files changed again
    monkeypatch.setattr(B, "time", later)
    normalized: list[str] = []
    real_fields = B._search_fields
    def counted(rel, fm, body):
        normalized.append(rel)
        return real_fields(rel, fm, body)

    monkeypatch.setattr(B, "_search_fields", counted)
    hits = B.search(tmp_path, "needle zzqq", top_k=None)
    assert B._snapshot(tmp_path).settled and held is not None
    assert sorted(normalized) == ["topics/c0.md", "topics/c2.md"]
    monkeypatch.setattr(B, "_search_fields", real_fields)
    assert [(hit["path"], hit["score"]) for hit in hits] == [
        (hit["path"], hit["score"]) for hit in (B.drop_snapshots() or B.search(tmp_path, "needle zzqq", top_k=None))]
